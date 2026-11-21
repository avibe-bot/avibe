// A take recorded by the iOS App itself (ios/Avibe/NativeHoldCapture.swift) rather than through
// getUserMedia and Web Audio, which opened about 0.5 s (p90 1 s) after a hold on an iPhone. Only
// the capture step changes: the pipeline (lib/voiceRecording) segments, previews and sends the
// App's PCM as it does the page's own.
import { appVoiceEnabled } from './appVoice';
import {
  hasNativeVoiceCapture,
  nativeVoiceCaptureStart,
  nativeVoiceCaptureStop,
  nativeVoiceHoldAdopt,
  nativeVoicePermission,
  onNativeVoiceCapture,
  onNativeVoiceHold,
  type NativeVoiceCaptureEvent,
  type NativeVoiceHoldTimings,
} from './native';
import { VOICE_LEVEL_INTERVAL_MS } from './voiceActivity';
import type { VoiceCaptureAttempt, VoiceCaptureTrace, VoiceNativeEnd } from './voiceCaptureTrace';
import {
  VOICE_CAPTURE_STOP_TIMEOUT_MS,
  type VoicePcmCapture,
  type VoicePcmCaptureFactory,
  type VoicePcmCaptureHandlers,
} from './voiceRecording';

export const NATIVE_CAPTURE_SAMPLE_RATE = 16_000;

/** Samples per loudness reading, as the page's own capture reports them. */
const LEVEL_SAMPLES = Math.round(NATIVE_CAPTURE_SAMPLE_RATE * VOICE_LEVEL_INTERVAL_MS / 1000);

/** Whether, here, the App records every voice take, so that its audio session has one owner
 *  and the page makes no audio of its own: an App that records, with the instance's switch for
 *  it on (lib/appVoice). */
export const appRecordsEveryTake = (): boolean => appVoiceEnabled() && hasNativeVoiceCapture();

/** Whether the App records this take: it records every take here, and its microphone is
 *  allowed. Until the user has answered the microphone question the take is WebKit's, which
 *  asks it. */
export const nativeCaptureAllowed = async (): Promise<boolean> => (
  appRecordsEveryTake() && (await nativeVoicePermission()) === 'granted'
);

/** The stream a take the App records stands on: no track of the page's own. */
export const emptyVoiceStream = (): MediaStream => (
  typeof MediaStream === 'function'
    ? new MediaStream()
    : { getTracks: () => [], getAudioTracks: () => [] } as unknown as MediaStream
);

const NATIVE_ENDS = new Set<VoiceNativeEnd>(['interrupted', 'route', 'background', 'gone', 'replaced', 'action_button']);
const nativeEnd = (reason: string | undefined): VoiceNativeEnd => (
  NATIVE_ENDS.has(reason as VoiceNativeEnd) ? reason as VoiceNativeEnd : 'other'
);

/** The most samples handed on at once: 100 ms, what the App sends at a time. A take the App
 *  began at touch-down arrives first as everything since then, in one event (over a second of
 *  audio); live recognition takes frames, not one of any size. */
const MAX_SAMPLES_AT_ONCE = NATIVE_CAPTURE_SAMPLE_RATE / 10;

/** Readings held back to be paced out: a chunk's worth, about 100 ms. */
const MAX_QUEUED_LEVELS = 8;

const newTakeId = (): string =>
  `h${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;

const decodePcm = (base64: string): Int16Array<ArrayBuffer> | null => {
  try {
    const binary = atob(base64);
    const samples = new Int16Array(new ArrayBuffer(binary.length - (binary.length % 2)));
    for (let index = 0; index < samples.length; index += 1) {
      const value = binary.charCodeAt(index * 2) | (binary.charCodeAt(index * 2 + 1) << 8);
      samples[index] = value >= 0x8000 ? value - 0x10000 : value;
    }
    return samples;
  } catch {
    return null;
  }
};

const bridgeErrorName = (error: unknown): string => {
  const code = (error as { code?: unknown; message?: unknown } | null)?.code;
  const message = (error as { message?: unknown } | null)?.message;
  if (code === 'failed' && message === 'permission') return 'NotAllowedError';
  if (code === 'failed' && message === 'busy') return 'NotReadableError';
  return 'other';
};

/** The name of the error a capture rejects with when the App no longer has the hold's take it
 *  was to take (dropped after its wait, or ended by the system): the page starts its own. */
export const NATIVE_HOLD_GONE = 'NativeHoldGoneError';

export const isNativeHoldGone = (error: unknown): boolean => (
  Boolean(error) && typeof error === 'object' && (error as { name?: unknown }).name === NATIVE_HOLD_GONE
);

const count = (value: unknown): number | undefined => (
  typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : undefined
);

/** The App's own timings of a hold, as the capture trace keeps them (ms from touch-down, except
 *  the two durations). Unknown values are left out. */
const holdTimings = (timings: NativeVoiceHoldTimings | undefined): Partial<VoiceCaptureAttempt> => {
  if (!timings) return {};
  const variant = timings.variant;
  const fields: Partial<VoiceCaptureAttempt> = {
    ...(variant === 'early' || variant === 'quiet' || variant === 'late' ? { hold: variant } : {}),
    holdRecMs: count(timings.recognisedAt),
    holdLiveMs: count(timings.liveAt),
    holdBufMs: count(timings.firstBufferAt),
    holdAdoptMs: count(timings.adoptedAt),
    holdUpMs: count(timings.releasedAt),
    holdActMs: count(timings.activateMs),
    holdStartMs: count(timings.startMs),
  };
  for (const key of Object.keys(fields) as (keyof VoiceCaptureAttempt)[]) {
    if (fields[key] === undefined) delete fields[key];
  }
  return fields;
};

export class NativePcmCapture implements VoicePcmCapture {
  private readonly handlers: VoicePcmCaptureHandlers;
  private readonly stopTimeoutMs: number;
  private readonly trace: VoiceCaptureTrace | null;
  private readonly take: string;
  /** The take is one the App began itself, at the hold's touch-down: taken, not started. */
  private readonly adopted: boolean;
  private attempt: VoiceCaptureAttempt | null = null;
  private unsubscribe: (() => void) | null = null;
  private unsubscribeHold: (() => void) | null = null;
  private stopTimer: ReturnType<typeof setTimeout> | null = null;
  private levelSum = 0;
  private levelCount = 0;
  private levels: number[] = [];
  private levelTimer: ReturnType<typeof setTimeout> | null = null;
  /** When the next paced reading is due (`Date.now()`). */
  private levelDue = 0;
  private started = false;
  private stopping = false;
  private stopped = false;

  constructor(handlers: VoicePcmCaptureHandlers, stopTimeoutMs: number, trace?: VoiceCaptureTrace, adopt?: string) {
    this.handlers = handlers;
    this.stopTimeoutMs = stopTimeoutMs;
    this.trace = trace ?? null;
    this.take = adopt ?? newTakeId();
    this.adopted = adopt !== undefined;
  }

  async start(): Promise<void> {
    if (this.started) throw new Error('native voice capture already started');
    this.started = true;
    // Stopped before it began (the page hid while the hold was checked): the App is never asked.
    if (this.stopping || this.stopped) return;
    // Listening before the App starts: its first audio can come before the start's answer.
    this.unsubscribe = onNativeVoiceCapture((event) => this.handleEvent(event));
    if (!this.unsubscribe) throw new Error('native voice capture unavailable');
    const attempt = this.trace?.attempt('native', emptyVoiceStream()) ?? null;
    this.attempt = attempt;
    if (attempt) attempt.sampleRate = NATIVE_CAPTURE_SAMPLE_RATE;
    const requestedAt = performance.now();
    // The take's microphone is the App's: its start is what the trace's `micMs` and `mic` say.
    const traceRequestedAt = this.trace?.mark() ?? 0;
    try {
      if (this.adopted) {
        // The App's timings of this hold, when the take ends, for the trace.
        this.unsubscribeHold = onNativeVoiceHold((event) => {
          if (event.take === this.take && event.phase === 'timings' && attempt) Object.assign(attempt, holdTimings(event.timings));
        });
        const taken = await nativeVoiceHoldAdopt(this.take);
        if (attempt) Object.assign(attempt, holdTimings(taken.timings));
      } else {
        const started = await nativeVoiceCaptureStart(this.take);
        // The App's own steps: activating its session, and starting the microphone.
        if (attempt) Object.assign(attempt, holdTimings({ activateMs: started.activateMs, startMs: started.engineMs }));
      }
    } catch (error) {
      const failure = Object.assign(new Error('native voice capture did not start'), {
        name: this.adopted ? NATIVE_HOLD_GONE : bridgeErrorName(error),
      });
      this.trace?.micSettled(traceRequestedAt, failure);
      if (attempt) {
        attempt.micMs = Math.round(performance.now() - requestedAt);
        attempt.mic = failure.name;
        this.trace?.ended(attempt, 'error', null);
      }
      this.release();
      this.stopped = true;
      throw failure;
    }
    this.trace?.micSettled(traceRequestedAt);
    if (attempt) {
      attempt.micMs = Math.round(performance.now() - requestedAt);
      attempt.mic = 'ok';
    }
    if (this.stopping || this.stopped) {
      // Stopped while the App was starting: it ends the take now.
      void nativeVoiceCaptureStop(this.take);
      if (this.stopped) this.release();
      return;
    }
    this.handlers.onCaptureState?.('live');
  }

  stop(): void {
    if (this.stopping || this.stopped) return;
    this.stopping = true;
    if (!this.unsubscribe) {
      this.finish();
      return;
    }
    // What the App still holds comes before its end.
    this.stopTimer = setTimeout(() => this.finish(), this.stopTimeoutMs);
    void nativeVoiceCaptureStop(this.take);
  }

  private handleEvent(event: NativeVoiceCaptureEvent): void {
    if (event.take !== this.take || this.stopped) return;
    if (event.pcm) {
      const samples = decodePcm(event.pcm);
      for (let from = 0; samples && from < samples.length; from += MAX_SAMPLES_AT_ONCE) {
        this.deliver(samples.slice(from, from + MAX_SAMPLES_AT_ONCE));
      }
    }
    if (event.ended) {
      if (!this.stopping) {
        this.stopping = true;
        const reason = nativeEnd(event.reason);
        if (this.attempt) {
          this.attempt.nativeEnd = reason;
          this.trace?.ended(this.attempt, 'error', null);
        }
        this.handlers.onError(new Error(`native voice capture ended: ${reason}`));
      }
      this.finish();
    }
  }

  private deliver(samples: Int16Array<ArrayBuffer>): void {
    if (this.attempt) this.trace?.frames(this.attempt);
    this.handlers.onSamples(samples);
    if (!this.handlers.onLevel) return;
    for (const sample of samples) {
      const value = sample / 0x8000;
      this.levelSum += value * value;
      this.levelCount += 1;
      if (this.levelCount >= LEVEL_SAMPLES) {
        this.queueLevel(Math.sqrt(this.levelSum / this.levelCount));
        this.levelSum = 0;
        this.levelCount = 0;
      }
    }
  }

  /** The App sends about 100 ms at a time: its readings are paced out one per
   *  `VOICE_LEVEL_INTERVAL_MS`, as the page's own capture gives them, not in a burst. */
  private queueLevel(rms: number): void {
    this.levels.push(rms);
    if (this.levels.length > MAX_QUEUED_LEVELS) this.levels.shift();
    if (!this.levelTimer) {
      this.levelDue = Date.now();
      this.nextLevel();
    }
  }

  private nextLevel(): void {
    const rms = this.levels.shift();
    if (rms === undefined || this.stopped) {
      this.levelTimer = null;
      return;
    }
    if (this.attempt) this.trace?.level(this.attempt);
    this.handlers.onLevel?.(rms);
    // Against a deadline: a timer that fires late does not let the readings fall behind the audio.
    const now = Date.now();
    this.levelDue = Math.max(this.levelDue + VOICE_LEVEL_INTERVAL_MS, now);
    this.levelTimer = setTimeout(() => this.nextLevel(), Math.max(0, this.levelDue - now));
  }

  private finish(): void {
    if (this.stopped) return;
    this.stopped = true;
    // An attempt the App ended itself is already `error`; `ended` keeps the first.
    if (this.attempt) {
      this.trace?.ended(this.attempt, this.attempt.chunks > 0 ? 'rendered' : 'stopped', null);
    }
    this.release();
    this.handlers.onStopped();
  }

  private release(): void {
    if (this.stopTimer) clearTimeout(this.stopTimer);
    this.stopTimer = null;
    if (this.levelTimer) clearTimeout(this.levelTimer);
    this.levelTimer = null;
    this.levels = [];
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.unsubscribeHold?.();
    this.unsubscribeHold = null;
  }
}

/** The pipeline's `createCapture` for a take the App records; the stream it is given is empty. */
export const nativeCaptureFactory: VoicePcmCaptureFactory = (
  _stream,
  sampleRate,
  _segmentSamples,
  handlers,
  stopTimeoutMs,
  trace,
) => {
  if (sampleRate !== NATIVE_CAPTURE_SAMPLE_RATE) throw new Error('native voice capture records 16 kHz only');
  return new NativePcmCapture(handlers, stopTimeoutMs ?? VOICE_CAPTURE_STOP_TIMEOUT_MS, trace);
};

/** The same for a hold the App recognised itself: its capture takes that take (`voice.holdAdopt`),
 *  whose audio since touch-down comes first. */
export const adoptedCaptureFactory = (take: string): VoicePcmCaptureFactory => (
  _stream,
  sampleRate,
  _segmentSamples,
  handlers,
  stopTimeoutMs,
  trace,
) => {
  if (sampleRate !== NATIVE_CAPTURE_SAMPLE_RATE) throw new Error('native voice capture records 16 kHz only');
  return new NativePcmCapture(handlers, stopTimeoutMs ?? VOICE_CAPTURE_STOP_TIMEOUT_MS, trace, take);
};
