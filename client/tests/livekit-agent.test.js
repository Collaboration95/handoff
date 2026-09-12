import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { ConnectionState, ParticipantKind, Room, RoomEvent, TokenSource } from 'livekit-client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAgent, useSession } from '@livekit/components-react';
import { ParticipantInfo, ParticipantInfo_State } from '@livekit/protocol';

// Characterize the installed SDK contract on which the client's failure toast relies.
// Room and participant events are real; only network prewarming is disabled.
const AGENT_STATE = 'lk.agent.state';
const CONNECT_TIMEOUT = 1000;
let root;
let room;
let agent;

function Probe() {
  const session = useSession(
    TokenSource.literal({
      serverUrl: 'wss://offline-test.invalid',
      participantToken: 'offline-test',
    }),
    { room, agentConnectTimeoutMilliseconds: CONNECT_TIMEOUT }
  );
  agent = useAgent(session);
  return createElement('output', null, agent.state);
}

async function setConnectionState(state) {
  await act(async () => {
    room.state = state;
    room.emit(RoomEvent.ConnectionStateChanged, state);
  });
}

function participantInfo(state, attributes = {}) {
  return new ParticipantInfo({
    sid: 'PA_test_agent',
    identity: 'test-agent',
    name: 'Test Agent',
    kind: ParticipantKind.AGENT,
    state: ParticipantInfo_State.ACTIVE,
    attributes: { [AGENT_STATE]: state, ...attributes },
  });
}

async function joinAgent(state) {
  let participant;
  await act(async () => {
    // Feed the same ingress used by server participant updates so Room installs
    // its real participant-to-room attribute event forwarding.
    participant = room.getOrCreateParticipant('test-agent', participantInfo(state));
  });
  return participant;
}

async function passDeadline() {
  await act(async () => {
    vi.advanceTimersByTime(CONNECT_TIMEOUT + 1);
  });
}

beforeEach(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  room = new Room();
  vi.spyOn(room, 'prepareConnection').mockResolvedValue(undefined);
  root = createRoot(document.createElement('div'));
  await act(async () => root.render(createElement(Probe)));
  await setConnectionState(ConnectionState.Connected);
});

afterEach(async () => {
  await act(async () => root.unmount());
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('LiveKit agent readiness', () => {
  it('recognizes an already-listening agent discovered after mount without an attributes event', async () => {
    await joinAgent('listening');
    expect(agent.state).toBe('listening');
    await passDeadline();
    expect(agent.isConnected).toBe(true);
    expect(agent.failureReasons).toBeNull();
  });

  it('preserves ready state when the participant emits an unrelated attribute delta', async () => {
    const participant = await joinAgent('initializing');
    await act(async () => participant.updateInfo(participantInfo('listening')));
    expect(agent.state).toBe('listening');
    await act(async () =>
      participant.updateInfo(participantInfo('listening', { custom: 'changed' }))
    );
    expect(agent.state).toBe('listening');
    expect(agent.attributes.custom).toBe('changed');
    await passDeadline();
    expect(agent.failureReasons).toBeNull();
  });

  it('still fails when no agent joins before the deadline', async () => {
    await passDeadline();
    expect(agent.state).toBe('failed');
    expect(agent.failureReasons).toContain('Agent did not join the room.');
  });

  it('still fails when an agent joins but never becomes ready', async () => {
    await joinAgent('initializing');
    await passDeadline();
    expect(agent.state).toBe('failed');
    expect(agent.failureReasons).toContain(
      'Agent joined the room but did not complete initializing.'
    );
  });

  it('clears an expired attempt and recognizes the listening agent after reconnecting', async () => {
    await passDeadline();
    expect(agent.state).toBe('failed');
    await setConnectionState(ConnectionState.Disconnected);
    expect(agent.state).toBe('disconnected');
    await setConnectionState(ConnectionState.Connected);
    await joinAgent('listening');
    await passDeadline();
    expect(agent.state).toBe('listening');
    expect(agent.failureReasons).toBeNull();
  });

  it('reports an unexpected agent departure instead of treating the room as ready', async () => {
    const participant = await joinAgent('listening');
    await act(async () => {
      room.remoteParticipants.delete(participant.identity);
      room.emit(RoomEvent.ParticipantDisconnected, participant);
    });
    expect(agent.state).toBe('failed');
    expect(agent.failureReasons).toContain('Agent left the room unexpectedly.');
  });
});
