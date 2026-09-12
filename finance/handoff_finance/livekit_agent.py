from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import Agent, AgentSession, RunContext, llm, room_io
from livekit.agents.voice import io
from livekit.plugins.openai.realtime import GPTLiveModel


SAMPLE_RATE = 24_000
CHANNELS = 1
FRAME_SIZE_MS = 20
BLOCKSIZE = 480
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_ROOM_NAME = "handoff-finance"
DEFAULT_MODEL = "gpt-live-1"
_PROPOSAL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class FinanceAPIError(RuntimeError):
    pass


def validate_finance_api_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("FINANCE_API_URL must be an http(s) loopback URL")
    return value.rstrip("/")


def _validate_proposal_id(proposal_id: str) -> str:
    if not _PROPOSAL_ID.fullmatch(proposal_id):
        raise FinanceAPIError("invalid proposal id")
    return proposal_id


Transport = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]


class FinanceAPI:
    def __init__(
        self,
        base_url: str = DEFAULT_API_URL,
        *,
        transport: Transport | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        self.base_url = validate_finance_api_url(base_url)
        self._transport = transport
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            if self._transport is not None:
                result = await self._transport(method, path, payload)
            else:
                if self._session is None:
                    self._session = aiohttp.ClientSession(timeout=self._timeout)
                async with self._session.request(
                    method, f"{self.base_url}{path}", json=payload
                ) as response:
                    if response.status >= 400:
                        raise FinanceAPIError(f"finance backend returned HTTP {response.status}")
                    result = await response.json(content_type=None)
        except FinanceAPIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
            raise FinanceAPIError("finance backend unavailable") from None
        if not isinstance(result, dict):
            raise FinanceAPIError("finance backend returned an invalid response")
        return result

    async def propose_invoice(self, objective: str, case_name: str = "showcase") -> dict[str, Any]:
        if not objective.strip():
            raise FinanceAPIError("invoice objective is required")
        return await self._request(
            "POST",
            "/v1/proposals",
            {"objective": objective, "case_name": case_name, "fault_injection": False},
        )

    async def get_invoice_status(self, proposal_id: str) -> dict[str, Any]:
        proposal_id = _validate_proposal_id(proposal_id)
        return await self._request("GET", f"/v1/proposals/{proposal_id}")

    async def create_approved_invoice(self, proposal_id: str) -> dict[str, Any]:
        proposal_id = _validate_proposal_id(proposal_id)
        return await self._request(
            "POST", "/v1/invoices/create", {"proposal_id": proposal_id}
        )

    async def voice_status(
        self,
        *,
        connected: bool,
        room_name: str,
        human_microphones: int,
        model: str,
        error: str | None,
    ) -> None:
        await self._request(
            "POST",
            "/v1/voice/status",
            {
                "connected": connected,
                "room_name": room_name,
                "human_microphones": human_microphones,
                "model": model,
                "error": error,
            },
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class ParticipantFrames(AsyncIterator[rtc.AudioFrame]):
    def __init__(self, source: Any) -> None:
        self.source = source
        self._closed = False

    def __aiter__(self) -> ParticipantFrames:
        return self

    async def __anext__(self) -> rtc.AudioFrame:
        event = await anext(self.source)
        return event.frame

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.source.aclose()


class MixedRoomInput(io.AudioInput):
    def __init__(self, mixer: rtc.AudioMixer) -> None:
        super().__init__(label="three-human-room-mix")
        self._mixer = mixer

    async def __anext__(self) -> rtc.AudioFrame:
        return await anext(self._mixer)


def _default_stream_factory(participant: rtc.RemoteParticipant) -> rtc.AudioStream:
    return rtc.AudioStream.from_participant(
        participant=participant,
        track_source=rtc.TrackSource.SOURCE_MICROPHONE,
        sample_rate=SAMPLE_RATE,
        num_channels=CHANNELS,
        frame_size_ms=FRAME_SIZE_MS,
    )


class HumanAudioMixer:
    """Owns the one-stream-per-human lifecycle feeding a public AudioMixer."""

    def __init__(
        self,
        room: rtc.Room,
        *,
        mixer: rtc.AudioMixer | None = None,
        stream_factory: Callable[[Any], Any] = _default_stream_factory,
        on_count_change: Callable[[int], None] | None = None,
    ) -> None:
        self.room = room
        self.mixer = mixer or rtc.AudioMixer(
            SAMPLE_RATE, CHANNELS, blocksize=BLOCKSIZE, stream_timeout_ms=100
        )
        self._stream_factory = stream_factory
        self._on_count_change = on_count_change
        self._active: dict[str, ParticipantFrames] = {}
        self._retired: list[ParticipantFrames] = []
        self._close_tasks: set[asyncio.Task[None]] = set()
        self._handlers: dict[str, Callable[..., None]] = {}
        self._started = False

    @property
    def input(self) -> MixedRoomInput:
        return MixedRoomInput(self.mixer)

    @property
    def human_microphones(self) -> int:
        return len(self._active)

    def _is_human(self, participant: Any) -> bool:
        return (
            participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
            and self.room.remote_participants.get(participant.identity) is participant
        )

    def _is_microphone(self, participant: Any, publication: Any) -> bool:
        return (
            self._is_human(participant)
            and publication.kind == rtc.TrackKind.KIND_AUDIO
            and publication.source == rtc.TrackSource.SOURCE_MICROPHONE
        )

    def _notify_count(self) -> None:
        if self._on_count_change is not None:
            self._on_count_change(self.human_microphones)

    def _subscribe_participant(self, participant: Any) -> None:
        if not self._is_human(participant):
            return
        for publication in participant.track_publications.values():
            if not self._is_microphone(participant, publication):
                continue
            if not publication.subscribed:
                publication.set_subscribed(True)
            elif publication.track is not None and not publication.muted:
                self._attach(participant, publication)

    def _attach(self, participant: Any, publication: Any) -> None:
        if (
            participant.identity in self._active
            or not self._is_microphone(participant, publication)
            or publication.muted
        ):
            return
        frames = ParticipantFrames(self._stream_factory(participant))
        self._active[participant.identity] = frames
        self.mixer.add_stream(frames)
        self._notify_count()

    def _detach(self, participant: Any) -> None:
        frames = self._active.pop(participant.identity, None)
        if frames is None:
            return
        self.mixer.remove_stream(frames)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._retired.append(frames)
        else:
            task = loop.create_task(frames.aclose())
            self._close_tasks.add(task)
            task.add_done_callback(self._finish_close_task)
        self._notify_count()

    def _finish_close_task(self, task: asyncio.Task[None]) -> None:
        self._close_tasks.discard(task)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            task.result()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        handlers: dict[str, Callable[..., None]] = {
            "participant_connected": self._subscribe_participant,
            "participant_disconnected": self._detach,
            "track_published": lambda publication, participant: self._subscribe_participant(
                participant
            ),
            "track_unpublished": lambda publication, participant: self._detach(participant),
            "track_subscribed": lambda track, publication, participant: self._attach(
                participant, publication
            ),
            "track_unsubscribed": lambda track, publication, participant: self._detach(
                participant
            ),
            "track_muted": lambda participant, publication: self._detach(participant),
            "track_unmuted": lambda participant, publication: self._attach(
                participant, publication
            ),
        }
        for event, handler in handlers.items():
            self.room.on(event, handler)
        self._handlers = handlers
        for participant in self.room.remote_participants.values():
            self._subscribe_participant(participant)

    async def aclose(self) -> None:
        if self._started:
            for event, handler in self._handlers.items():
                self.room.off(event, handler)
            self._handlers.clear()
            self._started = False
        for identity in list(self._active):
            participant = self.room.remote_participants.get(identity)
            if participant is not None:
                self._detach(participant)
            else:
                frames = self._active.pop(identity)
                self.mixer.remove_stream(frames)
                self._retired.append(frames)
        for frames in self._retired:
            with contextlib.suppress(Exception):
                await frames.aclose()
        self._retired.clear()
        if self._close_tasks:
            await asyncio.gather(*self._close_tasks, return_exceptions=True)
            self._close_tasks.clear()
        await self.mixer.aclose()


def _safe_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    invoice = proposal.get("invoice")
    safe_invoice = None
    if isinstance(invoice, Mapping):
        safe_invoice = {
            "id": invoice.get("id") or invoice.get("invoice_id"),
            "state": invoice.get("state") or invoice.get("status"),
            "stage": invoice.get("stage"),
            "verified": invoice.get("verified") is True,
        }
    return {
        "id": proposal.get("id"),
        "status": proposal.get("status"),
        "objective": proposal.get("objective"),
        "approved": proposal.get("approved") is True,
        "invoice": safe_invoice,
        "error": proposal.get("error"),
    }


class ProposalAnnouncer:
    def __init__(self) -> None:
        self._last: dict[str, tuple[Any, ...]] = {}

    def message_for(self, proposal: Mapping[str, Any]) -> str | None:
        proposal_id = proposal.get("id")
        if not isinstance(proposal_id, str):
            return None
        invoice = proposal.get("invoice")
        verified = isinstance(invoice, Mapping) and invoice.get("verified") is True
        key = (
            proposal.get("status"),
            proposal.get("approved") is True,
            verified,
            proposal.get("error"),
        )
        if self._last.get(proposal_id) == key:
            return None
        self._last[proposal_id] = key
        status = proposal.get("status")
        if status == "ready":
            if proposal.get("approved") is True:
                return "The invoice workflow is approved. The sandbox draft may now be created."
            return (
                "The proposal is ready. Review it in the activity page and approve the "
                "invoice workflow there."
            )
        if status == "needs_review":
            return "The proposal needs human review before any sandbox draft can be created."
        if status == "creating":
            return "The approved sandbox draft is being created and checked."
        if status == "completed":
            if verified:
                return "The sandbox draft invoice was created and verified by readback."
            return (
                "Invoice creation returned without verified readback, so it is not "
                "verified as complete."
            )
        if status == "failed":
            return (
                "The sandbox draft operation failed. Check the activity page for the "
                "recorded stage."
            )
        if status == "cancelled":
            return "The invoice proposal was cancelled."
        return None


class ProposalPoller:
    def __init__(self, api: FinanceAPI, agent: Agent, *, interval: float = 1.0) -> None:
        self.api = api
        self.agent = agent
        self.interval = interval
        self.proposal_id: str | None = None
        self.announcer = ProposalAnnouncer()
        self._stop = asyncio.Event()

    def watch(self, proposal_id: str) -> None:
        self.proposal_id = proposal_id

    async def run(self) -> None:
        while not self._stop.is_set():
            if self.proposal_id:
                try:
                    proposal = await self.api.get_invoice_status(self.proposal_id)
                    if message := self.announcer.message_for(proposal):
                        self.agent.duplex_session.append_commentary(message)
                except FinanceAPIError:
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


class InvoiceAssistant(Agent):
    def __init__(self, api: FinanceAPI, poller: ProposalPoller | None = None) -> None:
        self.api = api
        self.poller = poller
        super().__init__(
            instructions=(
                "You are the concise voice assistant for a synthetic finance demo. "
                "Help the room describe an invoice objective for the September showcase case. "
                "Keep replies short. Use propose_invoice to create a backend proposal, then tell "
                "people to review and approve the workflow in the activity page. A spoken approval "
                "does not authorize anything because mixed audio has no speaker identity. You may "
                "call create_approved_invoice after the backend reports approval; the backend "
                "remains "
                "authoritative. Never invent invoice fields, approval, creation, or verification. "
                "Only say creation succeeded when status is completed and invoice.verified is true."
            )
        )

    def _watch(self, proposal: Mapping[str, Any]) -> None:
        proposal_id = proposal.get("id")
        if self.poller is not None and isinstance(proposal_id, str):
            self.poller.watch(proposal_id)

    @llm.function_tool
    async def propose_invoice(
        self, context: RunContext, objective: str, case_name: str = "showcase"
    ) -> str:
        """Create a reviewable invoice proposal from the user's objective."""
        proposal = await self.api.propose_invoice(objective, case_name)
        self._watch(proposal)
        return json.dumps(_safe_proposal(proposal), separators=(",", ":"))

    @llm.function_tool
    async def get_invoice_status(self, context: RunContext, proposal_id: str) -> str:
        """Read the authoritative status of an existing invoice proposal."""
        proposal = await self.api.get_invoice_status(proposal_id)
        self._watch(proposal)
        return json.dumps(_safe_proposal(proposal), separators=(",", ":"))

    @llm.function_tool
    async def create_approved_invoice(self, context: RunContext, proposal_id: str) -> str:
        """Create a sandbox draft after backend approval and validation."""
        proposal = await self.api.create_approved_invoice(proposal_id)
        self._watch(proposal)
        return json.dumps(_safe_proposal(proposal), separators=(",", ":"))


class VoiceStatusReporter:
    def __init__(self, api: FinanceAPI, room_name: str, model: str) -> None:
        self.api = api
        self.room_name = room_name
        self.model = model
        self.connected = False
        self.human_microphones = 0
        self.error: str | None = None
        self._changed = asyncio.Event()
        self._stop = asyncio.Event()

    def update(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self, key, value)
        self._changed.set()

    async def send(self) -> None:
        await self.api.voice_status(
            connected=self.connected,
            room_name=self.room_name,
            human_microphones=self.human_microphones,
            model=self.model,
            error=self.error,
        )

    async def run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(FinanceAPIError):
                await self.send()
            self._changed.clear()
            change_task = asyncio.create_task(self._changed.wait())
            stop_task = asyncio.create_task(self._stop.wait())
            done, pending = await asyncio.wait(
                {change_task, stop_task}, timeout=5.0, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done | pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def stop(self) -> None:
        self._stop.set()
        self._changed.set()


@dataclass(frozen=True)
class RuntimeConfig:
    livekit_url: str
    livekit_token: str
    openai_api_key: str
    finance_api_url: str = DEFAULT_API_URL
    room_name: str = DEFAULT_ROOM_NAME
    model: str = DEFAULT_MODEL
    delegate_model: str = "gpt-5.6-luna"

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        required = ("LIVEKIT_URL", "LIVEKIT_TOKEN", "OPENAI_API_KEY")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError("missing required environment variables: " + ", ".join(missing))
        livekit_url = os.environ["LIVEKIT_URL"]
        if urlparse(livekit_url).scheme not in {"ws", "wss"}:
            raise ValueError("LIVEKIT_URL must use ws:// or wss://")
        return cls(
            livekit_url=livekit_url,
            livekit_token=os.environ["LIVEKIT_TOKEN"],
            openai_api_key=os.environ["OPENAI_API_KEY"],
            finance_api_url=validate_finance_api_url(
                os.getenv("FINANCE_API_URL", DEFAULT_API_URL)
            ),
            room_name=os.getenv(
                "FINANCE_ROOM_NAME", os.getenv("LIVEKIT_ROOM", DEFAULT_ROOM_NAME)
            ),
            model=os.getenv("GPT_LIVE_MODEL", DEFAULT_MODEL),
            delegate_model=os.getenv("GPT_LIVE_DELEGATE_MODEL", "gpt-5.6-luna"),
        )


async def run_direct_room(config: RuntimeConfig) -> None:
    api = FinanceAPI(config.finance_api_url)
    room = rtc.Room()
    reporter = VoiceStatusReporter(api, config.room_name, config.model)
    status_task = asyncio.create_task(reporter.run())
    mixer: HumanAudioMixer | None = None
    session: AgentSession | None = None
    poller: ProposalPoller | None = None
    poll_task: asyncio.Task[None] | None = None
    disconnected = asyncio.Event()
    room.on("disconnected", lambda reason: disconnected.set())
    try:
        await room.connect(
            config.livekit_url,
            config.livekit_token,
            options=rtc.RoomOptions(auto_subscribe=False, connect_timeout=10.0),
        )
        reporter.update(connected=True, room_name=room.name or config.room_name)
        mixer = HumanAudioMixer(
            room, on_count_change=lambda count: reporter.update(human_microphones=count)
        )
        mixer.start()
        model = GPTLiveModel(
            model=config.model,
            delegation="responses",
            responses_options={
                "model": config.delegate_model,
                "max_output_tokens": 256,
                "parallel_tool_calls": False,
            },
            api_key=config.openai_api_key,
        )
        session = AgentSession(llm=model, max_tool_steps=2)
        session.input.audio = mixer.input
        assistant = InvoiceAssistant(api)
        poller = ProposalPoller(api, assistant)
        assistant.poller = poller
        poll_task = asyncio.create_task(poller.run())
        await session.start(
            agent=assistant,
            room=room,
            room_options=room_io.RoomOptions(
                audio_input=False,
                audio_output=True,
                text_input=False,
                close_on_disconnect=False,
            ),
            record=False,
        )
        await disconnected.wait()
    except Exception:
        reporter.update(connected=False, error="voice bridge stopped")
        with contextlib.suppress(FinanceAPIError):
            await reporter.send()
        raise
    finally:
        if poller is not None:
            poller.stop()
        if poll_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        if session is not None:
            await session.aclose()
        if mixer is not None:
            await mixer.aclose()
        reporter.update(connected=False, human_microphones=0)
        with contextlib.suppress(FinanceAPIError):
            await reporter.send()
        reporter.stop()
        await status_task
        await room.disconnect()
        await api.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Join a LiveKit room as the Handoff GPT-Live finance participant."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env.local"),
        help="dotenv file to load without overriding existing environment values",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate required settings without connecting",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(args.env_file, override=False)
    try:
        config = RuntimeConfig.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if args.check_config:
        print("voice configuration is present")
        return 0
    asyncio.run(run_direct_room(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
