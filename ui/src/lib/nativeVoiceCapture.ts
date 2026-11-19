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
import type { VoiceCaptureAttempt, VoiceCaptureTrace } from './voiceCaptureTrace';
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
    // Listening before the App starts: its first audio can come before the start's answer.
    this.unsubscribe = onNativeVoiceCapture((event) => this.handleEvent(event));
    if (!this.unsubscribe) throw new Error('native voice capture unavailable');
    const attempt = this.trace?.attempt('native', emptyVoiceStream()) ?? null;
    this.attempt = attempt;
    if (attempt) attempt.sampleRate = NATIVE_CAPTURE_SAMPLE_RATE;
    const requestedAt = performance.now();
    try {
      await nativeVoiceCaptureStart(this.take);
    } catch (error) {
      if (attempt) {
        attempt.micMs = Math.round(performance.now() - requestedAt);
        attempt.mic = bridgeErrorName(error);
        this.trace?.ended(attempt, 'error', null);
      }
      this.release();
      this.stopped = true;
      throw Object.assign(new Error('native voice capture did not start'), { name: bridgeErrorName(error) });
    }
    if (attempt) {
      attempt.micMs = Math.round(performance.now() - requestedAt);
      attempt.mic = 'ok';
    }
    if (this.stopping || this.stopped) {
      // Stopped while the App was starting: it ends the take now.
      void nativeVoiceCaptureStop(this.take);
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
        this.handlers.onError(new Error(`native voice capture ended: ${event.reason ?? 'unknown'}`));
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
        if (this.attempt) this.trace?.level(this.attempt);
        this.handlers.onLevel(Math.sqrt(this.levelSum / this.levelCount));
        this.levelSum = 0;
        this.levelCount = 0;
      }
    }
  }

  private finish(): void {
    if (this.stopped) return;
    this.stopped = true;
    if (this.attempt) {
      this.trace?.ended(this.attempt, this.attempt.chunks > 0 ? 'rendered' : 'stopped', null);
    }
    this.release();
    this.handlers.onStopped();
  }

  private release(): void {
    if (this.stopTimer) clearTimeout(this.stopTimer);
    this.stopTimer = null;
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
