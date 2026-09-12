# LiveKit voice bridge

The bridge joins the hosted LiveKit room as a normal participant using `LIVEKIT_URL` and a short-lived `LIVEKIT_TOKEN`. It does not require the LiveKit API key or API secret. One `AgentSession` sends one GPT-Live-1 audio output to the room. The three remote human microphone streams are resampled to 24 kHz mono, unwrapped to `AudioFrame`, and combined with the public LiveKit `AudioMixer` before being assigned to `session.input.audio`.

## Required configuration

Put these values in the ignored repository-root `.env.local`:

```dotenv
OPENAI_API_KEY=...
LIVEKIT_URL=wss://livekit.handoff-demo.test
LIVEKIT_TOKEN=...
LIVEKIT_ROOM=handoff-finance
FINANCE_API_URL=http://127.0.0.1:8000
```

The token service must issue the agent a unique identity and a token scoped to `handoff-finance` with room join, audio publish, audio subscribe, and data publish grants. The token determines the actual room; `LIVEKIT_ROOM` labels status and defaults to `handoff-finance`. `FINANCE_API_URL` is intentionally restricted to `localhost` or `127.0.0.1` over HTTP(S). Secrets are read only from the environment and are never logged or included in heartbeat payloads.

The current deployment endpoints are intended to be `https://app.handoff-demo.test` for human clients and `wss://livekit.handoff-demo.test` for the bridge. Confirm that those names resolve to the operator-provided host from every laptop and use an ordinarily trusted TLS certificate. The previous `172.20.10.2` connection-sheet address identified this Mac and is not proof of the other laptop's address. Do not bypass certificate validation.

Check the local configuration without making a network connection:

```bash
PYTHONPATH=finance .venv/bin/python -m handoff_finance.livekit_agent --env-file .env.local --check-config
```

The installed CLI help is:

```text
usage: livekit_agent.py [-h] [--env-file ENV_FILE] [--check-config]
```

## Launch

Start the finance API first from the repository root:

```bash
PYTHONPATH=finance .venv/bin/python -m uvicorn handoff_finance.app:app --host 127.0.0.1 --port 8000
curl --fail http://127.0.0.1:8000/v1/health
```

Then start the direct-room voice participant:

```bash
PYTHONPATH=finance .venv/bin/python -m handoff_finance.livekit_agent --env-file .env.local
```

Open the existing LiveKit Meet client on three separate laptops with three unique human identities in `handoff-finance`. Each person should publish one microphone and wear headphones to prevent echo and feedback. The activity page is the source of truth for proposal approval, creation state, and readback verification. The voice heartbeat posts the connected room, current remote human microphone count, model name, and a compact error state to `POST /v1/voice/status` on changes and every five seconds.

The voice tools can only create a proposal, read its status, or ask the backend to create a previously approved invoice. They have no approve or cancel operation. Initial approval through **Approve invoice workflow** authorizes creation of a sandbox draft after server validation; the page's create button is a fallback, not a second approval. The backend rejects stale, unapproved, or unverified proposals and owns the invoice fields and idempotency journal.

## Rehearsal checklist

- `/v1/health` reports OpenAI ready and a fresh LiveKit heartbeat after the bridge connects.
- All three human clients show unique identities, one microphone each, and headphones in use.
- The bridge heartbeat reaches `human_microphones: 3`; muting or leaving lowers the count, and unmuting or rejoining restores it.
- A natural invoice request creates a proposal, and the agent asks for approval in the activity page.
- Spoken approval alone causes no state change. The page approval changes the authoritative proposal state.
- Creation is announced as successful only when the proposal is `completed` and `invoice.verified` is true.
- A failed or partial Airwallex result is described as unverified and preserves its recorded stage or invoice ID in the activity page.

## Current limits

The released SDK mixer produces one mono waveform without speaker identity. Overlapping speech can be harder to understand, and no statement in that mix can identify an approver. The authenticated activity-page action supplies approval. The bridge intentionally subscribes only to remote standard-participant microphone tracks and never mixes another agent or its own published output.

This repository has not yet verified the operator's Tailscale hostname, hosted certificate, room token, or a live three-laptop rehearsal. GPT-Live access was checked separately; the bridge does not repeat that paid connectivity check during local tests.
