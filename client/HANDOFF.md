# Handoff call client

This React/Next.js client connects to the Handoff voice bridge in the shared `handoff-finance` room. It includes a live participant list, the local display name **Jazz Li**, and three clearly labeled simulated participants. The **Simulate people** switch controls the visual simulation; it generates no audio or LiveKit participants.

## Run locally

Use Node.js 24 and pnpm 9.15.9:

```sh
cd client
pnpm install --frozen-lockfile
cp .env.example .env.local
# Fill in server-side LiveKit credentials in .env.local.
pnpm dev --hostname 127.0.0.1 --port 3001
```

Open http://localhost:3001. Start the finance API and voice bridge using [the bridge guide](../finance/LIVEKIT.md). Leave `AGENT_NAME` empty for this direct-room bridge; `LIVEKIT_ROOM` must match the bridge's token room.

The token endpoint is for local development and rejects production use without the starter's explicit preview setting. Credentials remain in the ignored `.env.local`; they are never part of this repository or public browser variables.

## Agent readiness checks

The client requires `@livekit/components-react` 2.9.21 or later and its compatible `livekit-client` 2.18.2 peer. LiveKit's [agent attribute fix](https://github.com/livekit/components-js/pull/1307) lets the UI recognize an agent that is already listening when the user joins. Earlier versions could incorrectly end that call after the 20-second initialization timeout.

Run `pnpm test` for six offline checks against the installed session and agent hooks, including readiness on join, attribute updates, reconnecting, and legitimate failures. Run `pnpm build` to check the production client. For a live check, join an already-listening agent, stay connected beyond 30 seconds, then leave and rejoin without reloading the page.

## Source

Adapted from [LiveKit's agent-starter-react](https://github.com/livekit-examples/agent-starter-react) at revision `44b8a0ce82039018a1feb2c1bda43b5ada2ab24e`. The original README and MIT license are retained.
