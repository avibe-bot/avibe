// A take recorded by the iOS App itself (ios/Avibe/NativeHoldCapture.swift) rather than through
// getUserMedia and Web Audio, which opened about 0.5 s (p90 1 s) after a hold on an iPhone. Only
// the capture step changes: the pipeline (lib/voiceRecording) segments, previews and sends the
// App's PCM as it does the page's own.
import {
  nativeVoiceCaptureStart,
  nativeVoiceCaptureStop,
  onNativeVoiceCapture,
  type NativeVoiceCaptureEvent,
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

export class NativePcmCapture implements VoicePcmCapture {
  private readonly handlers: VoicePcmCaptureHandlers;
  private readonly stopTimeoutMs: number;
  private readonly trace: VoiceCaptureTrace | null;
  private readonly take = newTakeId();
  private attempt: VoiceCaptureAttempt | null = null;
  private unsubscribe: (() => void) | null = null;
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

  constructor(handlers: VoicePcmCaptureHandlers, stopTimeoutMs: number, trace?: VoiceCaptureTrace) {
    this.handlers = handlers;
    this.stopTimeoutMs = stopTimeoutMs;
    this.trace = trace ?? null;
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
      await nativeVoiceCaptureStart(this.take);
    } catch (error) {
      const failure = Object.assign(new Error('native voice capture did not start'), { name: bridgeErrorName(error) });
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
      if (samples && samples.length > 0) this.deliver(samples);
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
