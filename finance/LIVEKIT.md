# LiveKit voice bridge

The bridge joins the hosted LiveKit room as a normal participant using `LIVEKIT_URL` and a short-lived `LIVEKIT_TOKEN`. It does not require the LiveKit API key or API secret. One `AgentSession` sends one GPT-Live-1 audio output to the room. The two remote human microphone streams are resampled to 24 kHz mono, unwrapped to `AudioFrame`, and combined with the public LiveKit `AudioMixer` before being assigned to `session.input.audio`.

## Required configuration

Put these values in the ignored repository-root `.env.local`:

```dotenv
OPENAI_API_KEY=...
LIVEKIT_URL=wss://livekit.handoff-demo.test
LIVEKIT_TOKEN=...
LIVEKIT_ROOM=handoff-finance
FINANCE_API_URL=http://127.0.0.1:8000
FINANCE_FAULT_INJECTION=false
```

The token service must issue the agent a unique identity and a token scoped to `handoff-finance` with room join, audio publish, audio subscribe, and data publish grants. The token determines the actual room; `LIVEKIT_ROOM` labels status and defaults to `handoff-finance`. `FINANCE_API_URL` is intentionally restricted to `localhost` or `127.0.0.1` over HTTP(S). Secrets are read only from the environment and are never logged or included in heartbeat payloads.

`FINANCE_FAULT_INJECTION` is an operator-only `true`/`false` switch. Leave it `false` for the normal voice path. Set it to `true` before launch for the staged stale-draft repair demonstration, then restart the bridge. The model cannot set or override this value. The staged result is a simulated input error, not a real Airwallex failure.

The active demo path uses LiveKit Cloud. Use the public `wss://…livekit.cloud` value stored as `LIVEKIT_URL`; no Tailscale, custom DNS, local certificate authority, or TLS bypass is needed. Never place a room token in documentation, chat, source control, or a shared URL.

To refresh a six-hour token from the server credentials in `.env.local`, load the environment and write the token directly to a private temporary file. Change both values for each participant; use two distinct human identities plus `handoff-finance-agent`. Never reuse an identity while it is connected.

```bash
set -a
source .env.local
set +a
umask 077
export TOKEN_IDENTITY=finance-member-1
TOKEN_FILE=/private/tmp/finance-member-1.token
PYTHONPATH=finance .venv/bin/python -c 'import os,sys; from datetime import timedelta; from livekit import api; room=os.environ.get("FINANCE_ROOM_NAME") or os.environ.get("LIVEKIT_ROOM","handoff-finance"); token=(api.AccessToken().with_identity(os.environ["TOKEN_IDENTITY"]).with_ttl(timedelta(hours=6)).with_grants(api.VideoGrants(room_join=True,room=room,can_publish=True,can_subscribe=True,can_publish_data=True)).to_jwt()); sys.stdout.write(token)' > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
```

Repeat the standard-participant command with `finance-member-2`. The command prints nothing to the terminal.

Mint the agent token separately so stock LiveKit clients recognize it as the Handoff agent and the SDK can maintain `lk.agent.state`:

```bash
export TOKEN_IDENTITY=handoff-finance-agent
TOKEN_FILE=/private/tmp/handoff-finance-agent.token
PYTHONPATH=finance .venv/bin/python -c 'import os,sys; from datetime import timedelta; from livekit import api; room=os.environ.get("FINANCE_ROOM_NAME") or os.environ.get("LIVEKIT_ROOM","handoff-finance"); token=(api.AccessToken().with_identity(os.environ["TOKEN_IDENTITY"]).with_name("Handoff").with_kind("agent").with_ttl(timedelta(hours=6)).with_grants(api.VideoGrants(room_join=True,room=room,can_publish=True,can_subscribe=True,can_publish_data=True,can_update_own_metadata=True)).to_jwt()); sys.stdout.write(token)' > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
```

`with_kind("agent")` marks this direct-room participant for LiveKit's agent hooks. `can_update_own_metadata=True` lets RoomIO publish `lk.agent.state`; it does not grant worker registration. Keep the stable identity `handoff-finance-agent`. Delete all temporary token files after the rehearsal.

The React starter's `/api/token` route must issue human tokens for the fixed room `process.env.LIVEKIT_ROOM || "handoff-finance"`; a random room isolates the client from the agent. Its `useAgent` hook recognizes only `ParticipantKind.AGENT` and reads `lk.agent.state`, so both agent-token settings above are required. The verified hosted participant reports name `Handoff`, kind `PARTICIPANT_KIND_AGENT`, and state `listening`.

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

On each of two laptops, open the stock LiveKit Meet custom tab at [meet.livekit.io/custom](https://meet.livekit.io/custom). Paste the same public Cloud WebSocket URL and that person's separate room-token file contents. Both tokens must target `handoff-finance`, and the identities must be unique. Each person should publish one microphone and wear headphones to prevent echo and feedback. The activity page is the source of truth for proposal approval, creation state, and readback verification. The voice heartbeat posts the connected room, current remote human microphone count, model name, and a compact error state to `POST /v1/voice/status` on changes and every five seconds.

The voice tools can only create a proposal, read its status, or ask the backend to create a previously approved invoice. They have no approve or cancel operation. Initial approval through **Approve invoice workflow** authorizes creation of a sandbox draft after server validation; the page's create button is a fallback, not a second approval. The backend rejects stale, unapproved, or unverified proposals and owns the invoice fields and idempotency journal.

## Rehearsal checklist

- `/v1/health` reports OpenAI ready and a fresh LiveKit heartbeat after the bridge connects.
- Both human clients show unique identities, one microphone each, and headphones in use.
- The bridge heartbeat reaches `human_microphones: 2`; muting or leaving lowers the count, and unmuting or rejoining restores it.
- A natural invoice request creates a proposal, and the agent asks for approval in the activity page.
- Spoken approval alone causes no state change. The page approval changes the authoritative proposal state.
- Creation is announced as successful only when the proposal is `completed` and `invoice.verified` is true.
- A failed or partial Airwallex result is described as unverified and preserves its recorded stage or invoice ID in the activity page.

## Current limits

The released SDK mixer produces one mono waveform without speaker identity. Overlapping speech can be harder to understand, and no statement in that mix can identify an approver. The authenticated activity-page action supplies approval. The bridge intentionally subscribes only to remote standard-participant microphone tracks and never mixes another agent or its own published output.

The supplied Cloud token was verified by joining `handoff-finance` as `handoff-finance-agent` with subscriptions disabled. A synthetic macOS speech clip was converted to signed 16-bit, 24 kHz mono PCM, sent as 20 ms frames to GPT-Live, and caused the Responses delegate to call a harmless in-memory tool. The full media path was then verified with two synthetic remote standard participants publishing microphone tracks: health reached `human_microphones: 2`, GPT-Live heard the spoken request through the mixer, and the finance tool created pending simulated-fault proposal `fd96f4b313ad40adb084e7e89a211c64`. It remained unapproved with no invoice. A separate no-tool observer received substantial agent speech audio from the room: 2,419 nonzero samples with peak signed-16 amplitude 3,853, above the 2,000-sample/500-peak proof threshold. The ignored evidence file is `finance/runs/livekit-media-proof-2026-09-12.json`. A live two-laptop human rehearsal remains.
