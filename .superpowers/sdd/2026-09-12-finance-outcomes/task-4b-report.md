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
- `finance/tests/test_livekit_agent.py`
  - 13 focused mixer, lifecycle, configuration, API-route, error-redaction, tool-surface, and announcement tests.
- `finance/LIVEKIT.md`
  - Exact direct-room launch, health check, three-laptop/headphone rehearsal, token grants, current hostname caveat, and mixed-audio limitations.

## Verification

Run from the repository root on 2026-09-12:

```text
PYTHONPATH=finance .venv/bin/pytest finance/tests -q
85 tests passed; one upstream Starlette/AnyIO deprecation warning

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

## Remaining external rehearsal

No hosted connection was attempted in this task. The operator still needs to provide a current reachable Tailscale hostname/address and a participant token for room `handoff-finance`. The ordinary TLS trust chain must validate `wss://livekit.handoff-demo.test`; the bridge has no certificate-bypass option. After those values exist, run the checklist in `finance/LIVEKIT.md` and verify the heartbeat reaches three human microphones.
