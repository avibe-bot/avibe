import {
  BUILTIN_BACKENDS, NATIVE_SETUP_BACKENDS, type AgentBackendId, type NativeCliBackend,
} from '@/lib/agentBackends';

/** The CLI assistants setup detects, installs and enables, in C6's order. */
export const ASSISTANT_ORDER = NATIVE_SETUP_BACKENDS;
export type AssistantId = NativeCliBackend;

/**
 * Every assistant setup draws a card for, in the order the cards stand: the built-in
 * coordinator first, then the CLI assistants it hands the work to. The intro, the
 * provider destinations and the assistants screen are one row of these, so the count
 * is read from here and never restated.
 */
export const SETUP_LINEUP: readonly AgentBackendId[] = [...BUILTIN_BACKENDS, ...ASSISTANT_ORDER];

const WORK = 1500;
const HANDOFF = 750;
const RETURN = 1000;
const SUMMARY = 1900;

/**
 * The authored relay from the approved Welcome design: each assistant works for
 * 1500ms, each handoff travels its wire, and the closing summary returns to the
 * coordinator. A step keeps its SOURCE active while the pulse is in transit; the
 * destination only activates once the pulse lands. The last card's wire is the return.
 */
type RelayStep = { id: string; active: number; duration: number; wire?: number };
const RELAY: RelayStep[] = [
  ...SETUP_LINEUP.flatMap((id, index): RelayStep[] => [
    { id, active: index, duration: WORK },
    index < SETUP_LINEUP.length - 1
      ? { id: `to-${SETUP_LINEUP[index + 1]}`, active: index, duration: HANDOFF, wire: index }
      : { id: 'return', active: index, duration: RETURN, wire: index },
  ]),
  { id: 'summary', active: 0, duration: SUMMARY },
];

/** The wire index the timeline gives the return from the last card to the first. */
export const RETURN_WIRE = SETUP_LINEUP.length - 1;

export const COLLABORATION_STEPS = RELAY.map((step, index) => {
  const start = RELAY.slice(0, index).reduce((total, previous) => total + previous.duration, 0);
  return { ...step, start, end: start + step.duration };
});
export const COLLABORATION_DURATION = COLLABORATION_STEPS[COLLABORATION_STEPS.length - 1].end;

/** Each card fills its skeleton in 850ms; the rest of its turn holds the completed state. */
export const WORK_CONTENT_DURATION = 850;
/** Both the document and the code skeleton reveal six lines, 110ms apart. */
export const WORK_LINES = 6;
const WORK_STARTS = SETUP_LINEUP.map((_, index) => COLLABORATION_STEPS[index * 2].start);

const clamp = (value: number) => Math.max(0, Math.min(1, value));

export interface CollaborationFrame {
  /** Index of the card that currently owns the work, or null when nothing is running. */
  active: number | null;
  /** Wire index the pulse is travelling, with its 0..1 position along that wire. */
  handoff: { wire: number; progress: number } | null;
  /** Skeleton fill 0..1 per card. */
  progress: number[];
  /** Completed work stays complete for the rest of the cycle, including the summary. */
  done: boolean[];
  /** Revealed skeleton lines per card. */
  written: number[];
  /** The closing phase, where the coordinator summarizes instead of restating its own work. */
  summary: boolean;
  /** True while the result is travelling back to, or being summarized by, the coordinator. */
  returning: boolean;
}

/** All cards and traveling pulses are projections of this one clock. */
export function collaborationFrame(elapsed: number, reducedMotion = false): CollaborationFrame {
  const time = reducedMotion ? COLLABORATION_DURATION - 1 : elapsed % COLLABORATION_DURATION;
  const step = COLLABORATION_STEPS.find((candidate) => time < candidate.end)
    ?? COLLABORATION_STEPS[COLLABORATION_STEPS.length - 1];
  const worked = WORK_STARTS.map((start) => time - start);
  const done = worked.map((value) => reducedMotion || value >= WORK_CONTENT_DURATION);
  return {
    active: step.active,
    handoff: step.wire !== undefined && !reducedMotion
      ? { wire: step.wire, progress: clamp((time - step.start) / step.duration) }
      : null,
    progress: worked.map((value, index) => (done[index] ? 1 : clamp(value / WORK_CONTENT_DURATION))),
    done,
    written: worked.map((value, index) => (done[index] ? WORK_LINES
      : step.active === index ? Math.max(0, Math.min(WORK_LINES, Math.floor((value - 40) / 110) + 1))
        : 0)),
    summary: step.id === 'summary',
    returning: step.id === 'return' || step.id === 'summary',
  };
}
