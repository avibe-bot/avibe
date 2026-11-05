import { VOICE_LEVEL_INTERVAL_MS } from './voiceRecording';

/**
 * Whether a take held sound loud enough to be speech, from the loudness the recorder reports
 * every `VOICE_LEVEL_INTERVAL_MS` (RMS of the input, 0–1). Only the total is kept, never the
 * audio. Deliberately generous: soft speech at arm's length reads about −35 to −25 dBFS and a
 * quiet room −55 to −50, so a take counts as voiced well below normal speech.
 */
export const VOICE_ACTIVITY_RMS = 0.01; // about −40 dBFS
/** This much sound in a take, in total, and it may hold speech. */
export const VOICE_ACTIVITY_MIN_MS = 400;

export type VoiceActivity = {
  /** One loudness reading, covering `VOICE_LEVEL_INTERVAL_MS` of audio. */
  observe: (rms: number) => void;
  /** Milliseconds of audio at or above `VOICE_ACTIVITY_RMS` so far. */
  voicedMs: () => number;
  voiced: () => boolean;
};

export const createVoiceActivity = (): VoiceActivity => {
  let voicedMs = 0;
  return {
    observe: (rms) => {
      if (Number.isFinite(rms) && rms >= VOICE_ACTIVITY_RMS) voicedMs += VOICE_LEVEL_INTERVAL_MS;
    },
    voicedMs: () => voicedMs,
    voiced: () => voicedMs >= VOICE_ACTIVITY_MIN_MS,
  };
};
