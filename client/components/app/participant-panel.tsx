'use client';

import { useEffect, useState } from 'react';
import { type Participant, Track } from 'livekit-client';
import { Mic, MicOff, Users } from 'lucide-react';
import {
  useIsSpeaking,
  useParticipantInfo,
  useParticipants,
  useSessionContext,
  useTrackMutedIndicator,
  useTracks,
} from '@livekit/components-react';
import { cn } from '@/lib/shadcn/utils';

const DEMO_PARTICIPANTS = [
  { id: 'demo-alex', name: 'Alex Chen', micOff: false },
  { id: 'demo-maya', name: 'Maya Patel', micOff: false },
  { id: 'demo-sam', name: 'Sam Taylor', micOff: true },
];

function ParticipantRow({ participant }: { participant: Participant }) {
  const { name, identity } = useParticipantInfo({ participant });
  const isSpeaking = useIsSpeaking(participant);
  const tracks = useTracks([Track.Source.Microphone], { onlySubscribed: false });
  const publication = tracks.find((track) => track.participant === participant)?.publication;
  const { isMuted } = useTrackMutedIndicator({
    participant,
    source: Track.Source.Microphone,
    publication,
  });
  const micOff = !publication || isMuted;

  return (
    <ParticipantDisplayRow
      name={name?.trim() || identity || 'Unnamed participant'}
      label={participant.isLocal ? 'You' : participant.isAgent ? 'Agent' : undefined}
      isSpeaking={isSpeaking}
      micOff={micOff}
    />
  );
}

function ParticipantDisplayRow({
  name,
  label,
  isSpeaking,
  micOff,
}: {
  name: string;
  label?: string;
  isSpeaking: boolean;
  micOff: boolean;
}) {
  const status = isSpeaking ? 'Speaking' : micOff ? 'Mic off' : 'Mic on';
  const Icon = micOff ? MicOff : Mic;

  return (
    <li className="flex items-center gap-3 rounded-lg px-3 py-2">
      <span
        aria-hidden="true"
        className={cn(
          'size-2 shrink-0 rounded-full bg-neutral-400',
          isSpeaking && 'bg-emerald-500'
        )}
      />
      <span className="min-w-0 flex-1 truncate text-sm" title={name}>
        {name}
        {label && <span className="text-muted-foreground"> · {label}</span>}
      </span>
      <span
        className={cn(
          'text-muted-foreground flex shrink-0 items-center gap-1.5 text-xs',
          isSpeaking && 'text-emerald-600 dark:text-emerald-400'
        )}
      >
        <Icon aria-hidden="true" className="size-3.5" />
        {status}
      </span>
    </li>
  );
}

export function ParticipantPanel() {
  const participants = useParticipants();
  const { isConnected } = useSessionContext();
  const [demoEnabled, setDemoEnabled] = useState(true);
  const [speakingTurn, setSpeakingTurn] = useState(0);
  const liveParticipants = isConnected ? participants : [];
  const demoCount = demoEnabled ? DEMO_PARTICIPANTS.length : 0;

  useEffect(() => {
    if (!demoEnabled) return;

    // Two speakers followed by a quiet interval. No audio is generated.
    const timer = window.setInterval(() => setSpeakingTurn((turn) => (turn + 1) % 3), 3500);
    return () => window.clearInterval(timer);
  }, [demoEnabled]);

  return (
    <details
      open
      className="bg-background/95 absolute top-3 right-3 left-3 z-[60] rounded-xl border shadow-sm backdrop-blur-sm sm:left-auto sm:w-80"
    >
      <summary className="cursor-pointer rounded-xl px-4 py-3 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2">
        <Users aria-hidden="true" className="mr-2 inline size-4" />
        Participants ({liveParticipants.length + demoCount})
      </summary>
      <div className="flex items-center justify-between gap-3 border-t px-4 py-2">
        <span className="text-muted-foreground text-xs">
          {liveParticipants.length} live · {demoCount} simulated
        </span>
        <label className="flex cursor-pointer items-center gap-2 text-xs font-medium">
          <input
            type="checkbox"
            role="switch"
            checked={demoEnabled}
            onChange={(event) => setDemoEnabled(event.target.checked)}
            className="accent-foreground size-4"
          />
          Simulate people
        </label>
      </div>
      <ul
        aria-label="Call participants"
        className="max-h-40 overflow-y-auto border-t p-1 sm:max-h-64"
      >
        {liveParticipants.map((participant) => (
          <ParticipantRow key={participant.identity} participant={participant} />
        ))}
        {demoEnabled &&
          DEMO_PARTICIPANTS.map((participant, index) => (
            <ParticipantDisplayRow
              key={participant.id}
              name={participant.name}
              label="Demo"
              micOff={participant.micOff}
              isSpeaking={!participant.micOff && speakingTurn === index}
            />
          ))}
      </ul>
      {!isConnected && (
        <p className="text-muted-foreground border-t px-4 py-2 text-xs">
          Join the call to appear here. Simulated people have no audio.
        </p>
      )}
    </details>
  );
}
