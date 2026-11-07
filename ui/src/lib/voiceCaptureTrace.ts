import { isNativeShell } from './native';
import { isStandalonePwa } from './platform';
import { createVoiceActivity } from './voiceActivity';
import { preferredVoiceSampleRate } from './voiceEngine';
import { emitVoiceTelemetry, type VoiceTelemetryOutcome } from './voiceTelemetry';

// One take's capture, from the press to the end of the microphone, for the reliability log
// (`capture_trace`): what the page, the microphone and each audio graph did, so a take that
// heard nothing says at which stage. States, counts and times only: no audio, no words, no
// device label or id.

/** Where the take started: the composer's microphone button, a press-and-hold, a Show Page. */
export type VoiceCaptureEntry = 'tap' | 'hold' | 'show_page';

/** What the page runs in: the native app, an installed (Home Screen) web app, a browser. */
export type VoiceCaptureSurface = 'app_shell' | 'standalone' | 'mobile_browser' | 'desktop_browser';

/**
 * What the microphone delivered. `no_frames`: the audio graph never rendered, so the take holds
 * no sound at all. `muted`: the system held the microphone back (a call, another app, Siri).
 * `silence`: sound arrived and none of it was speech. `sound`: it may hold speech.
 * `recorded`: recorded without Web Audio in a form this browser could not decode, so not judged.
 * `no_microphone`: the microphone was never opened.
 */
export type VoiceCaptureClass = 'no_microphone' | 'no_frames' | 'muted' | 'silence' | 'sound' | 'recorded';

/** How the take was recorded: the take's own audio graph, its rebuild when it did not start, a
 *  fresh context (when a graph rendered nothing, or a prewarmed one stalled), or a MediaRecorder
 *  without Web Audio (lib/voiceRecorderFallback). */
export type VoiceCaptureAttemptKind = 'initial' | 'start_rebuild' | 'context' | 'recorder';

/** A microphone track as the take saw it. */
export type VoiceTrackState = 'none' | 'ended' | 'live' | 'live_muted' | 'live_disabled' | 'live_muted_disabled';

/** Why a prewarmed audio context was not used for this take. */
export type VoicePrewarmDrop = 'hidden' | 'state' | 'constructor' | 'module_failed' | 'stalled' | 'rate';

const MIC_ERRORS = new Set([
  'NotAllowedError',
  'NotFoundError',
  'NotReadableError',
  'OverconstrainedError',
  'SecurityError',
  'AbortError',
  'InvalidStateError',
  'TypeError',
]);
const CONTEXT_STATES = new Set(['running', 'suspended', 'interrupted', 'closed']);
const AUDIO_SESSION_TYPES = new Set(['auto', 'playback', 'transient', 'transient-solo', 'ambient', 'play-and-record']);
const AUDIO_SESSION_STATES = new Set(['active', 'interrupted', 'inactive']);
/** The audio formats a MediaRecorder is asked about: whether this device can record the microphone
 *  without Web Audio (the fallback when its rendering stalls), and in what. */
const RECORDER_TYPES = ['audio/mp4', 'audio/webm', 'audio/ogg'] as const;
const recorderTypes = (): string[] => {
  const Recorder = (globalThis as { MediaRecorder?: { isTypeSupported?: (type: string) => boolean } }).MediaRecorder;
  if (!Recorder || typeof Recorder.isTypeSupported !== 'function') return [];
  return RECORDER_TYPES.filter((type) => {
    try {
      return Recorder.isTypeSupported!(type);
    } catch {
      return false;
    }
  });
};
/** Bounds on what one take can log. */
const MAX_ATTEMPTS = 6;
const MAX_TRACK_EVENTS = 24;

const now = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now());
const ms = (value: number): number => Math.max(0, Math.round(value));

// Page history since voice capture loaded: how often the page has been hidden (another app, a
// locked screen) and when it last was. On iOS the page's audio stops in the background.
let hiddenCount = 0;
let lastHiddenAt: number | null = null;
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'hidden') return;
    hiddenCount += 1;
    lastHiddenAt = now();
  });
}

/** The error's name if it is one a microphone request can fail with, else `other`. */
export const micErrorName = (error: unknown): string => {
  const name = error instanceof Error || error instanceof DOMException ? error.name : '';
  return MIC_ERRORS.has(name) ? name : 'other';
};

export const contextStateName = (context: { state: string }): string => (
  CONTEXT_STATES.has(context.state) ? context.state : 'other'
);

export const voiceCaptureSurface = (): VoiceCaptureSurface => {
  if (isNativeShell()) return 'app_shell';
  if (isStandalonePwa()) return 'standalone';
  const coarse = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(pointer: coarse)').matches;
  return coarse ? 'mobile_browser' : 'desktop_browser';
};

export const voiceTrackState = (stream: MediaStream | null | undefined): VoiceTrackState => {
  const track = stream?.getAudioTracks?.()[0];
  if (!track) return 'none';
  if (track.readyState === 'ended') return 'ended';
  if (track.muted && !track.enabled) return 'live_muted_disabled';
  if (track.muted) return 'live_muted';
  if (!track.enabled) return 'live_disabled';
  return 'live';
};

type AudioSessionReading = { type?: string; state?: string };

/** WebKit's `navigator.audioSession`, where it exists. */
const audioSession = (): AudioSessionReading | null => {
  if (typeof navigator === 'undefined') return null;
  const session = (navigator as Navigator & { audioSession?: AudioSessionReading }).audioSession;
  return session ?? null;
};

export type VoiceCaptureAttempt = {
  kind: VoiceCaptureAttemptKind;
  /** When it started, from the press. */
  atMs: number;
  /** A fresh microphone (`microphone`): how long it took, and `ok` or the error's name. */
  micMs?: number;
  mic?: string;
  /** The audio context when it was made (or found, prewarmed), and after it was asked to render. */
  contextState?: string;
  resumedState?: string;
  resumeMs?: number;
  sampleRate?: number;
  /** The capture worklet: `prewarmed`, `ok` or `failed`, and how long it took to load. */
  module?: 'prewarmed' | 'ok' | 'failed';
  moduleMs?: number;
  /** Chunks of frames the graph delivered, the first one's time from this attempt's start, and
   *  the loudness readings it sent. */
  chunks: number;
  firstFrameMs?: number;
  levels: number;
  /** Whether the graph renders at all: how far its context's clock moved (ms) from when it
   *  started to the attempt's end (a stalled engine reports `running` and stands still), how often the rendering thread called the capture worklet, and how
   *  many of those calls brought no input (the microphone's node delivered nothing to it). */
  clockAdvanceMs?: number;
  renderCalls?: number;
  emptyInputs?: number;
  /** The engine check on this graph's context (lib/voiceEngine): its clock moved, stood still
   *  while it ran, or it never ran to be judged. */
  engine?: 'healthy' | 'stalled' | 'unknown';
  trackStart: VoiceTrackState;
  trackEnd?: VoiceTrackState;
  /** How it ended: frames came, none came in the render window, its clock stood still, the
   *  context never rendered, the take stopped first, or it failed. */
  end?: 'rendered' | 'no_frames' | 'stalled' | 'not_running' | 'stopped' | 'error';
};

export type VoiceCaptureTraceEvent = {
  event: 'capture_trace';
  outcome: VoiceTelemetryOutcome;
  dictationId?: string;
  entry: VoiceCaptureEntry;
  surface: VoiceCaptureSurface;
  captureClass: VoiceCaptureClass;
  /** How the capture stopped: released, the page hid, discarded, or a capture failure. */
  stop?: 'finish' | 'hidden' | 'abort' | 'error';
  micMs?: number;
  mic?: string;
  totalMs: number;
  pageAgeMs: number;
  hiddenCount: number;
  sinceHiddenMs?: number;
  prewarm?: 'reused' | 'none';
  prewarmAgeMs?: number;
  prewarmDrop?: VoicePrewarmDrop;
  audioSession?: string;
  audioSessionState?: string;
  audioSessionStateEnd?: string;
  /** The audio formats this device's MediaRecorder can record; empty without one. */
  recorderTypes: string[];
  /** The page's real-time audio was known to stand still when the take started: it was recorded
   *  without Web Audio from its first moment. */
  pageStalled?: boolean;
  /** The sample rate the page had found rendering where the default stalled, when the take began. */
  preferredRate?: number;
  /** What carried the take after its context stalled: `L1` a fresh context at the same rate (the
   *  stalled one was stale), `L0` a context at another rate, `L4` a recording without Web Audio. */
  fallbackLevel?: 'L1' | 'L0' | 'L4';
  /** The rates whose contexts ran with their clocks standing still in this take. */
  stalledRates?: number[];
  /** The rate of the context whose graph carried the take's sound. */
  contextSampleRate?: number;
  /** Recorded without Web Audio: in what format, how many data events and bytes came, and how many
   *  segments were decoded to PCM or kept as files. */
  fallback?: 'recorder';
  recorderType?: string;
  recorderChunks?: number;
  recorderBytes?: number;
  decodedSegments?: number;
  fileSegments?: number;
  chunks: number;
  levels: number;
  peakDbfs?: number;
  /** The microphone's own events: [mute | unmute | ended, ms from the press]. */
  trackEvents: [string, number][];
  attempts: VoiceCaptureAttempt[];
};

/** One take's capture, filled as it happens and logged once when the take's capture ends. */
export class VoiceCaptureTrace {
  readonly entry: VoiceCaptureEntry;
  dictationId: string | undefined;
  private readonly startedAt = now();
  private readonly surface = voiceCaptureSurface();
  private readonly hiddenAtStart = hiddenCount;
  private readonly sinceHiddenMs = lastHiddenAt === null ? undefined : ms(now() - lastHiddenAt);
  private readonly audioSessionType: string | undefined;
  private readonly audioSessionStateStart: string | undefined;
  private readonly recorderTypes = recorderTypes();
  private micMs: number | undefined;
  private micResult: string | undefined;
  private prewarm: { reused: boolean; ageMs?: number; drop?: VoicePrewarmDrop } | undefined;
  private readonly attempts: VoiceCaptureAttempt[] = [];
  private readonly trackEvents: [string, number][] = [];
  private readonly watched = new Set<MediaStreamTrack>();
  private readonly activity = createVoiceActivity();
  private chunks = 0;
  private levels = 0;
  private mutedEver = false;
  private stalledAtStart = false;
  private readonly preferredRate = preferredVoiceSampleRate();
  private fallbackLevel: VoiceCaptureTraceEvent['fallbackLevel'];
  private readonly stalledRates = new Set<number>();
  private recorder: { type?: string; chunks: number; bytes: number; decoded: number; files: number } | null = null;
  private stopKind: VoiceCaptureTraceEvent['stop'];
  private emitted = false;

  constructor(entry: VoiceCaptureEntry, dictationId?: string) {
    this.entry = entry;
    this.dictationId = dictationId;
    const session = audioSession();
    this.audioSessionType = session?.type && AUDIO_SESSION_TYPES.has(session.type) ? session.type : undefined;
    this.audioSessionStateStart = session?.state && AUDIO_SESSION_STATES.has(session.state) ? session.state : undefined;
  }

  /** The take's microphone request: settled `ok`, or failed with `error`. */
  micSettled(requestedAt: number, error?: unknown): void {
    this.micMs = ms(now() - requestedAt);
    this.micResult = error === undefined ? 'ok' : micErrorName(error);
  }

  /** A start time for `micSettled`. */
  mark(): number {
    return now();
  }

  /** How the take's first audio context was found. */
  prewarmed(reused: boolean, ageMs?: number, drop?: VoicePrewarmDrop): void {
    this.prewarm = { reused, ...(ageMs !== undefined ? { ageMs: ms(ageMs) } : {}), ...(drop ? { drop } : {}) };
  }

  /** A new audio graph on `stream`. */
  attempt(kind: VoiceCaptureAttemptKind, stream: MediaStream): VoiceCaptureAttempt {
    // A graph is built on an open microphone, whoever opened it.
    this.micResult ??= 'ok';
    this.watchTrack(stream);
    const state = voiceTrackState(stream);
    if (state === 'live_muted' || state === 'live_muted_disabled') this.mutedEver = true;
    const attempt: VoiceCaptureAttempt = { kind, atMs: ms(now() - this.startedAt), chunks: 0, levels: 0, trackStart: state };
    if (this.attempts.length < MAX_ATTEMPTS) this.attempts.push(attempt);
    return attempt;
  }

  /** The attempt's start, for its own timings. */
  attemptStart(attempt: VoiceCaptureAttempt): number {
    return this.startedAt + attempt.atMs;
  }

  /** A chunk of frames from `attempt`'s graph (or its recording). */
  frames(attempt: VoiceCaptureAttempt): void {
    if (attempt.chunks === 0) attempt.firstFrameMs = ms(now() - this.attemptStart(attempt));
    attempt.chunks += 1;
  }

  level(attempt: VoiceCaptureAttempt): void {
    attempt.levels += 1;
    this.levels += 1;
  }

  /** A chunk of the take's PCM, as the pipeline took it in: counted, and judged by the silence
   *  gate like the take's own (lib/voiceActivity). */
  observePcm(samples: Int16Array): void {
    this.chunks += 1;
    this.activity.observePcm(samples);
  }

  ended(attempt: VoiceCaptureAttempt, end: NonNullable<VoiceCaptureAttempt['end']>, stream: MediaStream | null): void {
    if (attempt.end) return;
    attempt.end = end;
    attempt.trackEnd = voiceTrackState(stream);
  }

  /** The take started on a page whose real-time audio was known to stand still. */
  pageStalled(): void {
    this.stalledAtStart = true;
  }

  rescued(level: NonNullable<VoiceCaptureTraceEvent['fallbackLevel']>): void {
    this.fallbackLevel = level;
  }

  /** A context at `sampleRate` ran with its clock standing still. */
  stalledRate(sampleRate: number): void {
    if (Number.isFinite(sampleRate) && sampleRate > 0) this.stalledRates.add(sampleRate);
  }

  /** The take is now recorded without Web Audio, as `type` (the browser's default without one). */
  recorderStarted(type: string | undefined): void {
    this.micResult ??= 'ok';
    this.recorder ??= { chunks: 0, bytes: 0, decoded: 0, files: 0 };
    if (type && RECORDER_TYPES.includes(type as (typeof RECORDER_TYPES)[number])) this.recorder.type = type;
  }

  recorderData(bytes: number): void {
    if (!this.recorder) return;
    this.recorder.chunks += 1;
    this.recorder.bytes += Math.max(0, Math.round(bytes));
  }

  recorderSegment(decoded: boolean): void {
    if (!this.recorder) return;
    if (decoded) this.recorder.decoded += 1;
    else this.recorder.files += 1;
  }

  stoppedBy(stop: NonNullable<VoiceCaptureTraceEvent['stop']>): void {
    this.stopKind ??= stop;
  }

  private watchTrack(stream: MediaStream): void {
    for (const track of stream.getAudioTracks?.() ?? []) {
      if (this.watched.has(track)) continue;
      this.watched.add(track);
      for (const type of ['mute', 'unmute', 'ended'] as const) {
        track.addEventListener(type, () => {
          if (type === 'mute') this.mutedEver = true;
          if (this.trackEvents.length < MAX_TRACK_EVENTS) this.trackEvents.push([type, ms(now() - this.startedAt)]);
        });
      }
    }
  }

  captureClass(): VoiceCaptureClass {
    if (this.micResult !== 'ok') return 'no_microphone';
    // A gate that saw sound saw it whatever the track says.
    const sound = !this.activity.silent() && this.activity.peak() > 0;
    if (sound) return 'sound';
    if (this.mutedEver) return 'muted';
    // Audio this browser could not decode was not judged: it is transcribed as it is.
    if (this.recorder && this.recorder.files > 0) return 'recorded';
    return this.chunks === 0 ? 'no_frames' : 'silence';
  }

  toEvent(outcome: VoiceTelemetryOutcome): VoiceCaptureTraceEvent {
    const session = audioSession();
    const stateEnd = session?.state && AUDIO_SESSION_STATES.has(session.state) ? session.state : undefined;
    const peak = this.activity.peak();
    return {
      event: 'capture_trace',
      outcome,
      ...(this.dictationId ? { dictationId: this.dictationId } : {}),
      entry: this.entry,
      surface: this.surface,
      captureClass: this.captureClass(),
      ...(this.stopKind ? { stop: this.stopKind } : {}),
      ...(this.micMs !== undefined ? { micMs: this.micMs, mic: this.micResult } : {}),
      totalMs: ms(now() - this.startedAt),
      pageAgeMs: ms(now()),
      hiddenCount: this.hiddenAtStart,
      ...(this.sinceHiddenMs !== undefined ? { sinceHiddenMs: this.sinceHiddenMs } : {}),
      ...(this.prewarm
        ? {
            prewarm: this.prewarm.reused ? 'reused' as const : 'none' as const,
            ...(this.prewarm.ageMs !== undefined ? { prewarmAgeMs: this.prewarm.ageMs } : {}),
            ...(this.prewarm.drop ? { prewarmDrop: this.prewarm.drop } : {}),
          }
        : {}),
      ...(this.audioSessionType ? { audioSession: this.audioSessionType } : {}),
      ...(this.audioSessionStateStart ? { audioSessionState: this.audioSessionStateStart } : {}),
      ...(stateEnd ? { audioSessionStateEnd: stateEnd } : {}),
      recorderTypes: this.recorderTypes.slice(),
      ...(this.stalledAtStart ? { pageStalled: true } : {}),
      ...(this.preferredRate !== undefined ? { preferredRate: this.preferredRate } : {}),
      ...(this.fallbackLevel ? { fallbackLevel: this.fallbackLevel } : {}),
      ...(this.stalledRates.size ? { stalledRates: [...this.stalledRates].sort((a, b) => a - b) } : {}),
      ...(() => {
        const carried = [...this.attempts].reverse().find((attempt) => attempt.kind !== 'recorder' && attempt.chunks > 0);
        return carried?.sampleRate ? { contextSampleRate: carried.sampleRate } : {};
      })(),
      ...(this.recorder
        ? {
            fallback: 'recorder' as const,
            ...(this.recorder.type ? { recorderType: this.recorder.type } : {}),
            recorderChunks: this.recorder.chunks,
            recorderBytes: this.recorder.bytes,
            decodedSegments: this.recorder.decoded,
            fileSegments: this.recorder.files,
          }
        : {}),
      chunks: this.chunks,
      levels: this.levels,
      ...(this.chunks > 0 ? { peakDbfs: peak > 0 ? Math.max(-200, Math.min(0, Math.round(20 * Math.log10(peak)))) : -200 } : {}),
      trackEvents: this.trackEvents.slice(),
      attempts: this.attempts.map((attempt) => ({ ...attempt })),
    };
  }

  /** Logs the take's capture, once. */
  finish(outcome: VoiceTelemetryOutcome): void {
    if (this.emitted) return;
    this.emitted = true;
    try {
      emitVoiceTelemetry(this.toEvent(outcome));
    } catch {
      // Instrumentation cannot change what the take did.
    }
  }
}
