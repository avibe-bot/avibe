export const COLLABORATION_DURATION = 8900;
export const ASSISTANT_ORDER = ['claude', 'codex', 'opencode'] as const;
export type AssistantId = typeof ASSISTANT_ORDER[number];

/** All cards and traveling pulses are projections of this one clock. */
export function collaborationFrame(elapsed: number, reducedMotion = false) {
  const time = reducedMotion ? COLLABORATION_DURATION : elapsed % COLLABORATION_DURATION;
  const starts = [time >= 7000 ? 7000 : 0, 2250, 4500];
  const progress = starts.map((start) => Math.min(1, Math.max(0, (time - start) / 1500)));
  const active = reducedMotion ? null : time < 2250 ? 0 : time < 4500 ? 1 : time < 7000 ? 2 : 0;
  const handoff = time >= 1500 && time < 2250 ? { path: 0, progress: (time - 1500) / 750 }
    : time >= 3750 && time < 4500 ? { path: 1, progress: (time - 3750) / 750 }
      : time >= 6000 && time < 7000 ? { path: 2, progress: (time - 6000) / 1000 }
        : null;
  return { active, progress, handoff, summary: time >= 7000 };
}
