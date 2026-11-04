/**
 * The microphone's loudness while a take records, for a meter that reads it every frame. The
 * recording pipeline writes RMS (0–1) about every 32 ms; a reading older than `STALE_MS` is
 * silence (the microphone closed, or the page stopped delivering). Never stored or sent.
 */
export type VoiceLevelSource = {
  /** The newest loudness, RMS 0–1. */
  read: () => number;
  /** A fixed shape (bar heights, 0–1, oldest first) drawn still: a design frame's, or a test's. */
  still?: readonly number[];
  /** Draw the still shape as reduced motion does (one static volume bar at its newest height),
   *  whatever the device prefers: a design frame of that state. */
  reducedMotion?: boolean;
};

const STALE_MS = 250;

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());

export type VoiceLevel = VoiceLevelSource & {
  set: (rms: number) => void;
  reset: () => void;
};

export const createVoiceLevel = (): VoiceLevel => {
  let value = 0;
  let at = Number.NEGATIVE_INFINITY;
  return {
    read: () => (now() - at > STALE_MS ? 0 : value),
    set: (rms) => {
      value = Number.isFinite(rms) ? Math.min(1, Math.max(0, rms)) : 0;
      at = now();
    },
    reset: () => {
      value = 0;
      at = Number.NEGATIVE_INFINITY;
    },
  };
};

/** A still meter: these bar heights (0–1, oldest first), drawn without motion. */
export const stillVoiceLevel = (
  heights: readonly number[],
  options: { reducedMotion?: boolean } = {},
): VoiceLevelSource => ({
  read: () => 0,
  still: heights,
  ...(options.reducedMotion ? { reducedMotion: true } : {}),
});

/** How loud a reading looks on a meter, 0–1: decibels, from about −55 dBFS (a quiet room) to
 *  −12 dBFS (close, clear speech), so ordinary speech fills most of the range. */
export const voiceLevelHeight = (rms: number): number => {
  if (!(rms > 0)) return 0;
  const db = 20 * Math.log10(rms);
  return Math.min(1, Math.max(0, (db + 55) / 43));
};
