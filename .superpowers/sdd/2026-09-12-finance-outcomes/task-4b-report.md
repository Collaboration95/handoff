# Task 4b report — direct-room LiveKit + GPT-Live-1 bridge

## Delivered

- `finance/handoff_finance/livekit_agent.py`
  - Direct participant connection with `LIVEKIT_URL` + `LIVEKIT_TOKEN`; no LiveKit server API secret or worker registration.
  - Manual microphone-only subscription for remote `STANDARD` participants.
  - One `rtc.AudioStream.from_participant` per human, `AudioFrameEvent.frame` adapter, public `rtc.AudioMixer(24000, 1, blocksize=480)`, and `io.AudioInput` assigned before `AgentSession.start`.
  - Idempotent join/publish/subscribe/mute/unmute/unpublish/unsubscribe/disconnect lifecycle with immediate stream close and bounded shutdown.
  - One `GPTLiveModel(model="gpt-live-1", delegation="responses")` / `AgentSession` output.
  - Fixed loopback finance API client with a three-second total timeout and compact errors.
  - Exactly three model tools: propose, status, and create-after-backend-approval. There is no approve or cancel tool.
  - Once-per-change proposal announcements; completed is announced only with `invoice.verified is true`.
  - Voice status heartbeat on changes and every five seconds.
  - `connected=true` only after `AgentSession.start`; a fatal GPT-Live error marks the heartbeat unhealthy and stops the bridge.
  - Explicit model-owned HTTP client cleanup and camera/screen lifecycle filtering so non-microphone changes cannot detach a live mic.
  - Operator-only `FINANCE_FAULT_INJECTION=true|false`; the model cannot select the simulated stale-draft mode.
- `finance/tests/test_livekit_agent.py`
  - 17 focused mixer, lifecycle, configuration, API-route, error-redaction, tool-surface, health, and announcement tests.
- `finance/LIVEKIT.md`
  - Exact direct-room launch, Cloud client setup, safe token refresh, health check, three-laptop/headphone rehearsal, and mixed-audio limitations.

## Verification

Run from the repository root on 2026-09-12:

```text
PYTHONPATH=finance .venv/bin/pytest finance/tests -q
93 tests passed; one upstream Starlette/AnyIO deprecation warning

PYTHONPATH=finance .venv/bin/python -m py_compile \
  finance/handoff_finance/livekit_agent.py finance/tests/test_livekit_agent.py
exit 0

PYTHONPATH=finance .venv/bin/python -m handoff_finance.livekit_agent --help
usage: livekit_agent.py [-h] [--env-file ENV_FILE] [--check-config]

PYTHONPATH=finance LIVEKIT_URL=wss://livekit.handoff-demo.test \
  LIVEKIT_TOKEN=test-room-token OPENAI_API_KEY=test-openai-key \
  .venv/bin/python -m handoff_finance.livekit_agent \
  --env-file /dev/null --check-config
voice configuration is present
```

Installed imports compiled against `livekit-agents 1.8.1`, `livekit-plugins-openai 1.8.1`, `livekit 1.1.18`, and `openai 2.54.0`.

## Hosted integration evidence

- LiveKit Cloud join succeeded with subscriptions disabled: room `handoff-finance`, room SID `RM_9rm5epi6442e`, identity `handoff-finance-agent`, zero remote participants.
- An isolated `GPTLiveModel(model="gpt-live-1", delegation="responses")` session started in the Cloud room.
- macOS synthetic speech was converted to signed 16-bit, 24 kHz mono PCM and fed in 20 ms frames. GPT-Live understood the utterance and its Responses delegate called the harmless `integration_probe` in-memory tool. The clean rerun emitted `gpt_live_session_started` and `responses_noop_tool_called` with no HTTP client leak warning.
- After explicit user authorization for LiveKit participant microphone audio to OpenAI GPT-Live, the persistent bridge started as process/session ID `75114`. `/v1/health` reported `connected=true`, `stale=false`, room `handoff-finance`, model `gpt-live-1`, zero microphones, and no error.
- No proposal approval or invoice write was performed by these probes.

## Remaining rehearsal

Join three stock LiveKit Meet clients using unique Cloud room tokens and headphones. Confirm the heartbeat reaches three microphones and exercise mute, unmute, leave, and rejoin. The running bridge has not yet processed a live human turn. Coordinate with the finance operator before creating another proposal so an in-progress approved proposal is not superseded.
