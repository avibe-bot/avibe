import { VOICE_LEVEL_INTERVAL_MS } from './voiceRecording';
import { emitVoiceTelemetry, type VoiceTelemetryEvent } from './voiceTelemetry';

/**
 * Whether a take held sound loud enough to be speech, from the loudness the recorder reports
 * every `VOICE_LEVEL_INTERVAL_MS` (RMS of the input, 0–1). Only the total is kept, never the
 * audio. Deliberately generous: soft speech at arm's length reads about −35 to −25 dBFS and a
 * quiet room −55 to −50, so a take counts as voiced well below normal speech.
 */
export const VOICE_ACTIVITY_RMS = 0.01; // about −40 dBFS
/** Louder than this (about −35 dBFS) is taken as someone speaking, not a room's noise: a live
 *  transcript that stays empty through seconds of it is not working. */
export const VOICE_SPEECH_RMS = 0.018;
/** This much sound in a take, in total, and it may hold speech. */
export const VOICE_ACTIVITY_MIN_MS = 400;

/**
 * The silence gate. A take that never holds `VOICE_SILENCE_MIN_READINGS` readings at or above
 * this (about −44 dBFS) within one `VOICE_SILENCE_SPAN_READINGS` stretch held no speech:
 * recognition is not asked, because on near-silence it can read its hotwords back as text.
 * Deliberately loose (宁松勿紧): a syllable spans several readings, so speech whose whole take
 * is −47 dBFS, 33 dB under ordinary speech on an iPhone (−14), still clears it; a room with
 * nobody speaking measured −49 to −86 dBFS, and a click or a tap there is one reading.
 */
export const VOICE_SILENCE_RMS = 0.0063; // about −44 dBFS
/** Readings at or above the threshold that make a take speech... */
export const VOICE_SILENCE_MIN_READINGS = 3;
/** ...within this many consecutive readings (about a third of a second). */
export const VOICE_SILENCE_SPAN_READINGS = 10;

/** The recorder's PCM rate, and the samples one reading covers (`VOICE_LEVEL_INTERVAL_MS`). */
const PCM_SAMPLE_RATE = 16_000;
const READING_SAMPLES = Math.round((PCM_SAMPLE_RATE * VOICE_LEVEL_INTERVAL_MS) / 1000);

export type VoiceActivity = {
  /** One loudness reading, covering `VOICE_LEVEL_INTERVAL_MS` of audio. */
  observe: (rms: number) => void;
  /** The take's own 16 kHz PCM, as it is recorded: one reading per `VOICE_LEVEL_INTERVAL_MS`.
   *  The recorder hands over every sample it puts in a segment, the flushed tail included, so
   *  what is judged is exactly what would be transcribed. */
  observePcm: (samples: Int16Array) => void;
  /** Milliseconds of audio at or above `VOICE_ACTIVITY_RMS` so far. */
  voicedMs: () => number;
  voiced: () => boolean;
  /** The silence gate (`VOICE_SILENCE_RMS`): the take held no speech. False until a reading
   *  arrived: without levels nothing is gated. */
  silent: () => boolean;
  /** Readings at or above `VOICE_SILENCE_RMS`, and the loudest reading (RMS): what the gate
   *  reports when it drops a take, so a false block shows up in the logs. */
  loudReadings: () => number;
  peak: () => number;
};

export const createVoiceActivity = (): VoiceActivity => {
  let voicedMs = 0;
  let readings = 0;
  let loudReadings = 0;
  let peak = 0;
  let sound = false;
  // The indices of the last readings at or above the gate's threshold.
  const loud: number[] = [];
  // A reading's sum of squares carried across PCM chunks.
  let squares = 0;
  let counted = 0;
  const activity: VoiceActivity = {
    observePcm: (samples) => {
      for (let index = 0; index < samples.length; index += 1) {
        const sample = samples[index]! / 32_768;
        squares += sample * sample;
        counted += 1;
        if (counted === READING_SAMPLES) {
          activity.observe(Math.sqrt(squares / counted));
          squares = 0;
          counted = 0;
        }
      }
    },
    observe: (rms) => {
      if (!Number.isFinite(rms)) return;
      readings += 1;
      peak = Math.max(peak, rms);
      if (rms >= VOICE_ACTIVITY_RMS) voicedMs += VOICE_LEVEL_INTERVAL_MS;
      if (rms < VOICE_SILENCE_RMS) return;
      loudReadings += 1;
      if (sound) return;
      loud.push(readings);
      while (loud.length && loud[0]! <= readings - VOICE_SILENCE_SPAN_READINGS) loud.shift();
      if (loud.length >= VOICE_SILENCE_MIN_READINGS) sound = true;
    },
    voicedMs: () => voicedMs,
    voiced: () => voicedMs >= VOICE_ACTIVITY_MIN_MS,
    silent: () => readings > 0 && !sound,
    loudReadings: () => loudReadings,
    peak: () => peak,
  };
  return activity;
};

/** A take the silence gate kept away from recognition, for the reliability log: its length, how
 *  many readings reached the gate, and its loudest reading. Nothing that was said. */
export const voiceSilenceGateEvent = (activity: VoiceActivity, durationMs?: number): VoiceTelemetryEvent => ({
  event: 'dictation_gated',
  outcome: 'empty',
  providerStage: 'finalization',
  ...(durationMs !== undefined && durationMs >= 0 ? { totalDurationMs: Math.round(durationMs) } : {}),
  voicedReadings: activity.loudReadings(),
  peakDbfs: activity.peak() > 0 ? Math.max(-200, Math.min(0, Math.round(20 * Math.log10(activity.peak())))) : -200,
});

export const reportVoiceSilenceGate = (activity: VoiceActivity, durationMs?: number): void => {
  try {
    emitVoiceTelemetry(voiceSilenceGateEvent(activity, durationMs));
  } catch {
    // Instrumentation cannot change what the gate decided.
  }
};

