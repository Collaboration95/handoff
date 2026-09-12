from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from livekit import rtc
from livekit.agents import llm

import handoff_finance.livekit_agent as livekit_agent
from handoff_finance.livekit_agent import (
    FinanceAPI,
    FinanceAPIError,
    HumanAudioMixer,
    InvoiceAssistant,
    ProposalAnnouncer,
    RuntimeConfig,
    _close_voice_session,
    _bind_session_health,
    _safe_proposal,
    validate_finance_api_url,
)


@dataclass
class FakePublication:
    sid: str
    kind: int = rtc.TrackKind.KIND_AUDIO
    source: int = rtc.TrackSource.SOURCE_MICROPHONE
    muted: bool = False
    subscribed: bool = True
    track: object | None = field(default_factory=object)
    subscription_requests: list[bool] = field(default_factory=list)

    def set_subscribed(self, value: bool) -> None:
        self.subscription_requests.append(value)


@dataclass
class FakeParticipant:
    identity: str
    kind: int = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
    publication: FakePublication | None = None
    extra_publications: list[FakePublication] = field(default_factory=list)

    @property
    def track_publications(self):
        publications = [] if self.publication is None else [self.publication]
        publications.extend(self.extra_publications)
        return {publication.sid: publication for publication in publications}


class FakeRoom:
    def __init__(self, participants):
        self.remote_participants = {p.identity: p for p in participants}
        self.callbacks = {}

    def on(self, event, callback=None):
        def register(fn):
            self.callbacks.setdefault(event, []).append(fn)
            return fn

        return register(callback) if callback else register

    def off(self, event, callback):
        self.callbacks[event].remove(callback)

    def emit(self, event, *args):
        for callback in list(self.callbacks.get(event, [])):
            callback(*args)


class FakeStream:
    def __init__(self, participant):
        self.identity = participant.identity
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class FakeMixer:
    def __init__(self):
        self.added = []
        self.removed = []
        self.closed = False

    def add_stream(self, stream):
        self.added.append(stream)

    def remove_stream(self, stream):
        self.removed.append(stream)

    async def aclose(self):
        self.closed = True


def human(name: str) -> FakeParticipant:
    return FakeParticipant(name, publication=FakePublication(f"{name}-mic"))


def test_mixer_includes_three_humans_and_excludes_agent_participants() -> None:
    """Catches feeding agent output back into GPT Live or dropping a human mic."""
    people = [human("finance"), human("ops"), human("approver")]
    agent = FakeParticipant(
        "other-agent",
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT,
        publication=FakePublication("agent-mic"),
    )
    mixer = FakeMixer()
    room = FakeRoom([*people, agent])
    bridge = HumanAudioMixer(room, mixer=mixer, stream_factory=FakeStream)

    bridge.start()

    assert bridge.human_microphones == 3
    assert [stream.source.identity for stream in mixer.added] == [
        "finance",
        "ops",
        "approver",
    ]


def test_repeated_track_event_does_not_duplicate_participant_stream() -> None:
    """Catches duplicate PCM contribution after repeated subscription events."""
    participant = human("finance")
    mixer = FakeMixer()
    room = FakeRoom([participant])
    bridge = HumanAudioMixer(room, mixer=mixer, stream_factory=FakeStream)
    bridge.start()

    room.emit("track_subscribed", object(), participant.publication, participant)

    assert bridge.human_microphones == 1
    assert len(mixer.added) == 1


def test_direct_room_subscribes_only_remote_human_microphones() -> None:
    """Catches direct-room mode subscribing video, screen audio, agents, or its own track."""
    microphone = FakePublication("human-mic", subscribed=False, track=None)
    camera = FakePublication(
        "human-camera",
        kind=rtc.TrackKind.KIND_VIDEO,
        source=rtc.TrackSource.SOURCE_CAMERA,
        subscribed=False,
        track=None,
    )
    person = FakeParticipant("finance", publication=microphone, extra_publications=[camera])
    agent = FakeParticipant(
        "other-agent",
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT,
        publication=FakePublication("agent-mic", subscribed=False, track=None),
    )
    room = FakeRoom([person, agent])
    mixer = FakeMixer()
    bridge = HumanAudioMixer(room, mixer=mixer, stream_factory=FakeStream)

    bridge.start()
    local = human("local-agent-output")
    room.emit("track_unmuted", local, local.publication)

    assert microphone.subscription_requests == [True]
    assert camera.subscription_requests == []
    assert agent.publication.subscription_requests == []
    assert bridge.human_microphones == 0
    assert mixer.added == []


def test_mute_unmute_disconnect_and_close_release_streams() -> None:
    """Catches muted/disconnected audio lingering in the mix or leaking resources."""
    first = human("finance")
    second = human("ops")
    mixer = FakeMixer()
    room = FakeRoom([first, second])
    bridge = HumanAudioMixer(room, mixer=mixer, stream_factory=FakeStream)
    bridge.start()
    first_stream = mixer.added[0]

    first.publication.muted = True
    room.emit("track_muted", first, first.publication)
    first.publication.muted = False
    room.emit("track_unmuted", first, first.publication)
    room.emit("participant_disconnected", second)
    asyncio.run(bridge.aclose())

    assert first_stream in mixer.removed
    assert first_stream.source.closed is True
    assert bridge.human_microphones == 0
    assert mixer.closed is True
    assert all(not callbacks for callbacks in room.callbacks.values())


def test_runtime_mute_closes_removed_stream_without_waiting_for_shutdown() -> None:
    """Catches an unconsumed AudioStream queue leaking for the rest of the room session."""
    async def exercise():
        participant = human("finance")
        mixer = FakeMixer()
        room = FakeRoom([participant])
        bridge = HumanAudioMixer(room, mixer=mixer, stream_factory=FakeStream)
        bridge.start()
        stream = mixer.added[0]

        participant.publication.muted = True
        room.emit("track_muted", participant, participant.publication)
        await asyncio.sleep(0)

        assert stream.source.closed is True
        await bridge.aclose()

    asyncio.run(exercise())


def test_camera_lifecycle_does_not_remove_active_microphone() -> None:
    """Catches camera mute/unpublish events accidentally tearing down the human mic."""
    microphone = FakePublication("human-mic")
    camera = FakePublication(
        "human-camera",
        kind=rtc.TrackKind.KIND_VIDEO,
        source=rtc.TrackSource.SOURCE_CAMERA,
    )
    participant = FakeParticipant(
        "finance", publication=microphone, extra_publications=[camera]
    )
    mixer = FakeMixer()
    room = FakeRoom([participant])
    bridge = HumanAudioMixer(room, mixer=mixer, stream_factory=FakeStream)
    bridge.start()

    room.emit("track_muted", participant, camera)
    room.emit("track_unpublished", camera, participant)
    room.emit("track_unsubscribed", object(), camera, participant)

    assert bridge.human_microphones == 1
    assert mixer.removed == []


def test_voice_shutdown_closes_session_then_model_owned_http_client() -> None:
    """Catches the GPT-Live model-owned aiohttp session leaking at bridge shutdown."""
    events = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            events.append(self.name)

    asyncio.run(_close_voice_session(Resource("session"), Resource("model")))

    assert events == ["session", "model"]


def test_fatal_gpt_live_error_marks_heartbeat_unhealthy_and_stops_bridge() -> None:
    """Catches a dead GPT-Live session leaving the room heartbeat falsely green."""
    session = FakeRoom([])
    updates = []
    reporter = SimpleNamespace(update=lambda **changes: updates.append(changes))
    stop = asyncio.Event()
    unbind = _bind_session_health(session, reporter, stop)

    session.emit(
        "error", SimpleNamespace(error=SimpleNamespace(recoverable=False))
    )

    assert updates == [{"connected": False, "error": "gpt-live session error"}]
    assert stop.is_set()
    unbind()
    assert session.callbacks["error"] == []
    assert session.callbacks["close"] == []


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def proposal(status="ready", *, approved=False, verified=None):
    invoice = None if verified is None else {"id": "inv_123", "verified": verified}
    return {
        "id": "proposal_123",
        "status": status,
        "objective": "Invoice accepted September work",
        "case_name": "showcase",
        "fault_injection": False,
        "approved": approved,
        "decision": None,
        "invoice": invoice,
        "error": None,
    }


@asynccontextmanager
async def local_finance_api(handler, **options):
    application = web.Application()
    application.router.add_route("*", "/{path:.*}", handler)
    async with TestServer(application) as server:
        api = FinanceAPI(str(server.make_url("/")), **options)
        try:
            yield api
        finally:
            await api.aclose()


def test_slow_draft_creation_outlives_the_status_request_timeout() -> None:
    """Catches a successful multi-step draft write being reported as unavailable."""
    async def exercise():
        writes = []

        async def handle(request):
            assert request.method == "POST"
            assert request.path == "/v1/invoices/create"
            writes.append(await request.json())
            await asyncio.sleep(0.15)
            return web.json_response(proposal("completed", approved=True, verified=True))

        async with local_finance_api(handle, timeout_seconds=0.05) as api:
            result = await api.create_approved_invoice("proposal_123")

        assert result["status"] == "completed"
        assert result["invoice"]["verified"] is True
        assert writes == [{"proposal_id": "proposal_123"}]

    asyncio.run(exercise())


def test_create_timeout_reads_status_without_repeating_the_write() -> None:
    """Catches reporting an ambiguous write as failed or creating it a second time."""
    transport = FakeTransport(
        [asyncio.TimeoutError(), proposal("completed", approved=True, verified=True)]
    )
    api = FinanceAPI(transport=transport)

    result = asyncio.run(api.create_approved_invoice("proposal_123"))

    assert result["status"] == "completed"
    assert result["invoice"]["verified"] is True
    assert transport.calls == [
        ("POST", "/v1/invoices/create", {"proposal_id": "proposal_123"}),
        ("GET", "/v1/proposals/proposal_123", None),
    ]


@pytest.mark.parametrize("operation", ["status", "heartbeat"])
def test_status_and_heartbeat_keep_the_short_timeout(operation) -> None:
    """Catches draft creation's longer timeout slowing health and status checks."""
    async def exercise():
        async def handle(request):
            await asyncio.sleep(0.15)
            return web.json_response(proposal("creating", approved=True))

        async with local_finance_api(handle, timeout_seconds=0.05) as api:
            with pytest.raises(FinanceAPIError, match="timed out"):
                if operation == "status":
                    await api.get_invoice_status("proposal_123")
                else:
                    await api.voice_status(
                        connected=True, room_name="demo", human_microphones=1,
                        model="demo", error=None,
                    )

    asyncio.run(exercise())


def test_create_tool_surfaces_safe_backend_error_to_the_model() -> None:
    """Catches the LiveKit executor masking an actionable backend error as internal."""
    message = "billing_reference_already_written: Check existing proposal proposal_prior."
    api = FinanceAPI(transport=FakeTransport([FinanceAPIError(message)]))
    tool = next(
        tool for tool in InvoiceAssistant(api).tools if tool.info.name == "create_approved_invoice"
    )

    with pytest.raises(llm.ToolError) as raised:
        asyncio.run(tool(None, "proposal_123"))

    assert str(raised.value) == message


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            "approved_verified_proposal_required",
            "approved_verified_proposal_required",
        ),
        (
            {
                "code": "billing_reference_already_written",
                "existing_proposal_id": "proposal_prior",
                "invoice_id": "inv_prior",
                "private_data": "Bearer secret-token",
            },
            "A draft creation is already recorded for this billing reference.",
        ),
        ("Bearer secret-token", "finance backend returned HTTP 409"),
    ],
)
def test_conflict_errors_explain_known_codes_without_echoing_arbitrary_details(detail, expected):
    """Catches lost approval/duplicate reasons and accidental disclosure of error bodies."""
    async def exercise():
        async def handle(request):
            return web.json_response({"detail": detail}, status=409)

        async with local_finance_api(handle) as api:
            with pytest.raises(FinanceAPIError) as raised:
                await api.create_approved_invoice("proposal_123")
        message = str(raised.value)
        assert expected in message
        assert "secret-token" not in message
        if isinstance(detail, dict):
            assert "proposal_prior" in message
            assert "inv_prior" in message

    asyncio.run(exercise())


def test_finance_api_uses_only_locked_non_approval_routes() -> None:
    """Catches a voice tool gaining an approval/cancel route or model-chosen URL."""
    transport = FakeTransport(
        [proposal(), proposal(), proposal("completed", approved=True, verified=True)]
    )
    api = FinanceAPI("http://127.0.0.1:8000", transport=transport)

    async def exercise():
        await api.propose_invoice("Invoice accepted September work", "showcase")
        await api.get_invoice_status("proposal_123")
        await api.create_approved_invoice("proposal_123")

    asyncio.run(exercise())

    assert transport.calls == [
        (
            "POST",
            "/v1/proposals",
            {
                "objective": "Invoice accepted September work",
                "case_name": "showcase",
                "fault_injection": False,
            },
        ),
        ("GET", "/v1/proposals/proposal_123", None),
        ("POST", "/v1/invoices/create", {"proposal_id": "proposal_123"}),
    ]
    assert all("approve" not in path and "cancel" not in path for _, path, _ in transport.calls)
    assert {tool.info.name for tool in InvoiceAssistant(api).tools} == {
        "propose_invoice",
        "simulate_invoice_autofix",
        "get_invoice_status",
        "create_approved_invoice",
    }


def test_autofix_simulation_has_no_objective_or_creation_arguments_and_is_watched() -> None:
    """Simulation runs the fixed correction case without turning its fault into an objective."""
    transport = FakeTransport([proposal("checking")])
    api = FinanceAPI(transport=transport)
    watched = []
    assistant = InvoiceAssistant(api, SimpleNamespace(watch=watched.append))
    tool = next(tool for tool in assistant.tools if tool.info.name == "simulate_invoice_autofix")

    result = json.loads(asyncio.run(tool(None)))

    assert transport.calls == [("POST", "/v1/demo/autofix", None)]
    assert watched == ["proposal_123"]
    assert result["status"] == "checking"
    assert result["approved"] is False
    assert result["invoice"] is None


def repaired_proposal():
    value = proposal()
    value["decision"] = {
        "status": "repaired",
        "baseline": {"quantity": 10, "unit_price_minor": 10000, "currency": "SGD"},
        "selected": {"quantity": 6, "unit_price_minor": 9000, "currency": "SGD"},
        "baseline_checks": {"checks": [
            {"rule": "quantity", "passed": False},
            {"rule": "unit_price", "passed": False},
            {"rule": "currency", "passed": True},
        ]},
        "private_evidence": "not included in the voice summary",
    }
    return value


def test_voice_repair_summary_reports_only_actual_selected_correction() -> None:
    value = repaired_proposal()
    repair = _safe_proposal(value)["repair"]

    assert repair == {
        "status": "repaired",
        "autofixed": True,
        "baseline": {"quantity": 10, "unit_price_minor": 10000, "currency": "SGD"},
        "selected": {"quantity": 6, "unit_price_minor": 9000, "currency": "SGD"},
        "failed_checks": ["quantity", "unit_price"],
    }
    value["decision"]["status"] = "needs_review"
    assert _safe_proposal(value)["repair"]["autofixed"] is False
    value["decision"]["status"] = "repaired"
    value["decision"]["selected"] = None
    assert _safe_proposal(value)["repair"]["autofixed"] is False


def test_autofix_announcement_explains_corrected_proposal_without_claiming_creation() -> None:
    announcer = ProposalAnnouncer()
    value = repaired_proposal()

    message = announcer.message_for(value)

    assert "autofixed" in message.lower()
    assert "10 to 6" in message
    assert "SGD 100.00 to SGD 90.00" in message
    assert "No invoice has been created or sent" in message
    assert "approve" in message.lower()
    assert announcer.message_for(value) is None
    value["decision"]["status"] = "needs_review"
    other = ProposalAnnouncer().message_for(value)
    assert "autofixed" not in other.lower()


def test_operator_fault_mode_applies_to_voice_proposal_without_model_argument() -> None:
    """Catches the voice proposal losing the staged fault mode or exposing it to the model."""
    transport = FakeTransport([proposal()])
    api = FinanceAPI(
        "http://127.0.0.1:8000", transport=transport, fault_injection=True
    )

    asyncio.run(api.propose_invoice("Exercise the stale draft repair", "showcase"))

    assert transport.calls == [
        (
            "POST",
            "/v1/proposals",
            {
                "objective": "Exercise the stale draft repair",
                "case_name": "showcase",
                "fault_injection": True,
            },
        )
    ]
    propose_tool = next(
        tool for tool in InvoiceAssistant(api).tools if tool.info.name == "propose_invoice"
    )
    assert "fault_injection" not in str(propose_tool.info)


@pytest.mark.parametrize(
    "url",
    [
        "http://backend.internal:8000",
        "https://example.com",
        "http://127.0.0.1:8000@evil.test",
        "file:///tmp/socket",
    ],
)
def test_finance_api_rejects_non_loopback_targets(url: str) -> None:
    """Catches SSRF through environment or model-controlled backend URLs."""
    with pytest.raises(ValueError):
        validate_finance_api_url(url)


def test_api_errors_are_bounded_and_do_not_echo_sensitive_details() -> None:
    """Catches credentials or raw upstream bodies escaping through tool output."""
    transport = FakeTransport([RuntimeError("Bearer secret-token raw audio bytes")])
    api = FinanceAPI("http://localhost:8000", transport=transport)

    with pytest.raises(FinanceAPIError) as raised:
        asyncio.run(api.get_invoice_status("proposal_123"))

    assert str(raised.value) == "finance backend unavailable"


def test_status_announcements_are_once_only_and_require_verified_completion() -> None:
    """Catches repeated polling chatter and false success from partial external results."""
    announcer = ProposalAnnouncer()

    assert announcer.message_for(proposal("ready")) == (
        "The proposal is ready. Review it in the activity page and approve the "
        "invoice workflow there."
    )
    assert announcer.message_for(proposal("ready")) is None
    approved = announcer.message_for(proposal("ready", approved=True))
    assert approved == "The invoice workflow is approved. The sandbox draft may now be created."
    unsafe = announcer.message_for(proposal("completed", approved=True, verified=False))
    assert unsafe is not None
    assert "not verified" in unsafe.lower()
    assert "success" not in unsafe.lower()
    assert announcer.message_for(proposal("completed", approved=True, verified=False)) is None
    verified = announcer.message_for(proposal("completed", approved=True, verified=True))
    assert verified == "The sandbox draft invoice was created and verified by readback."


def test_reused_draft_voice_summary_does_not_claim_a_new_invoice() -> None:
    """Catches authoritative readback/reuse being announced as a second creation."""
    value = proposal("completed", approved=True, verified=True)
    value["invoice"]["reused_existing"] = True
    announcer = ProposalAnnouncer()

    assert _safe_proposal(value)["invoice"]["reused_existing"] is True
    assert announcer.message_for(value) == (
        "Existing sandbox draft verified; no new invoice was created."
    )
    assert announcer.message_for(value) is None
    value["invoice"]["verified"] = False
    assert "not verified" in announcer.message_for(value)


def test_direct_room_config_needs_token_but_not_server_api_secret(monkeypatch) -> None:
    """Catches accidental regression from participant mode to worker-secret credentials."""
    for name in (
        "LIVEKIT_URL",
        "LIVEKIT_TOKEN",
        "OPENAI_API_KEY",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit.handoff-demo.test")
    monkeypatch.setenv("LIVEKIT_TOKEN", "room-token")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-token")
    monkeypatch.setenv("FINANCE_FAULT_INJECTION", "true")

    config = RuntimeConfig.from_env()

    assert config.livekit_token == "room-token"
    assert config.fault_injection is True


def test_cli_reconnects_after_disconnect_and_error_with_capped_safe_backoff(monkeypatch, capsys):
    """Catches a closed room terminating the bridge instead of restoring its connection."""
    config = RuntimeConfig("wss://livekit.test", "room-secret", "openai-secret")
    outcomes = [None, RuntimeError("Bearer secret-token"), None, None, None, None, None]
    calls, delays, statuses, closed = [], [], [], []

    async def room_attempt(value):
        calls.append(value)
        if not outcomes:
            raise asyncio.CancelledError()
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def pause(delay):
        delays.append(delay)

    async def status(**payload):
        statuses.append(payload)

    async def close():
        closed.append(True)

    monkeypatch.setattr(livekit_agent, "run_direct_room", room_attempt)
    monkeypatch.setattr(livekit_agent.asyncio, "sleep", pause)
    monkeypatch.setattr(livekit_agent, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(RuntimeConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        livekit_agent, "FinanceAPI",
        lambda *args, **kwargs: SimpleNamespace(voice_status=status, aclose=close),
    )

    assert livekit_agent.main([]) == 130
    assert len(calls) == 8
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert len(statuses) == 7
    assert all(item["connected"] is False for item in statuses)
    assert all(item["human_microphones"] == 0 for item in statuses)
    assert "Reconnecting" in statuses[0]["error"]
    assert "secret" not in json.dumps(statuses)
    assert "secret" not in capsys.readouterr().out
    assert closed == [True]


def test_cli_keyboard_interrupt_does_not_retry(monkeypatch):
    """Catches an explicit operator stop entering the reconnect loop."""
    config = RuntimeConfig("wss://livekit.test", "room-secret", "openai-secret")
    calls = []

    async def room_attempt(value):
        calls.append(value)
        raise KeyboardInterrupt()

    monkeypatch.setattr(livekit_agent, "run_direct_room", room_attempt)
    monkeypatch.setattr(livekit_agent, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(RuntimeConfig, "from_env", classmethod(lambda cls: config))

    assert livekit_agent.main([]) == 130
    assert len(calls) == 1


def test_check_config_exits_without_entering_room(monkeypatch, capsys):
    """Catches configuration checks accidentally joining a room or starting retries."""
    config = RuntimeConfig("wss://livekit.test", "room-secret", "openai-secret")
    started = []
    monkeypatch.setattr(livekit_agent, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(RuntimeConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(livekit_agent, "run_voice_bridge", lambda value: started.append(value))

    assert livekit_agent.main(["--check-config"]) == 0
    assert capsys.readouterr().out == "voice configuration is present\n"
    assert started == []


def test_invalid_config_exits_without_retrying(monkeypatch):
    """Catches a missing required setting becoming an endless connection retry."""
    def invalid_config(cls):
        raise ValueError("missing required environment variables: LIVEKIT_TOKEN")

    started = []
    monkeypatch.setattr(livekit_agent, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(RuntimeConfig, "from_env", classmethod(invalid_config))
    monkeypatch.setattr(livekit_agent, "run_voice_bridge", lambda value: started.append(value))

    with pytest.raises(SystemExit, match="missing required environment variables"):
        livekit_agent.main([])
    assert started == []
