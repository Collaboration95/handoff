from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from livekit import rtc

from handoff_finance.livekit_agent import (
    FinanceAPI,
    FinanceAPIError,
    HumanAudioMixer,
    InvoiceAssistant,
    ProposalAnnouncer,
    RuntimeConfig,
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
        "get_invoice_status",
        "create_approved_invoice",
    }


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

    config = RuntimeConfig.from_env()

    assert config.livekit_token == "room-token"
