import {
  finalizeVoiceDictation,
  VOICE_SEGMENT_MS,
  VOICE_SETTLE_RETRY_WINDOW_MS,
  VOICE_TRANSCRIPTION_CONCURRENCY,
  VoiceTranscriptionError,
  VoiceTranscriptionQueue,
  type VoiceTranscriptionSegment,
} from './voiceTranscription';
import {
  claimVoiceCapture,
  VoiceRecordingPipeline,
  type VoiceCaptureClaim,
  type VoiceRecordingPipelineOptions,
} from './voiceRecording';
import { createVoiceActivity, reportVoiceSilenceGate } from './voiceActivity';
import { resolveVoiceContext, type VoiceContext, type VoiceContextSource } from './voiceContext';
import {
  VoiceRealtimeSession,
  type VoiceRealtimeOptions,
  type VoiceRealtimePreview,
} from './voiceRealtime';

type PendingVoiceSegment = VoiceTranscriptionSegment & {
  task: Promise<void>;
  transcriptionStarted?: boolean;
};

type VoicePipeline = Pick<VoiceRecordingPipeline, 'abort' | 'finish' | 'start'>;
type VoiceRealtime = Pick<VoiceRealtimeSession, 'abort' | 'finish' | 'sendPcm' | 'start'>;
type VoiceQueue = Pick<VoiceTranscriptionQueue, 'enqueue'>;

export type ShowPageVoiceDictationInput = {
  before: string;
  after: string;
  captureClaim?: VoiceCaptureClaim;
  maxFileBytes?: number | null;
  onPreview?: (text: string) => void;
  /** The same preview, with the settled text and the tail the recognizer may still revise apart. */
  onPreviewParts?: (preview: VoiceRealtimePreview) => void;
  /** The conversation this dictation is spoken into, for realtime recognition and its HTTP
   *  fallback alike; one still loading is waited for briefly (`resolveVoiceContext`). */
  context?: VoiceContextSource;
  /** The microphone's loudness (RMS, 0–1) as it records, for a meter. */
  onLevel?: (rms: number) => void;
};

export type ShowPageVoiceDictationDependencies = {
  getUserMedia?: () => Promise<MediaStream>;
  createPipeline?: (options: VoiceRecordingPipelineOptions) => VoicePipeline;
  createRealtime?: (options: VoiceRealtimeOptions) => VoiceRealtime;
  createQueue?: (options: {
    concurrency: number;
    signal: AbortSignal;
    retrySignal: AbortSignal;
    dictationId: string;
    context: Promise<VoiceContext>;
  }) => VoiceQueue;
  finalize?: typeof finalizeVoiceDictation;
  newDictationId?: () => string;
};

const newDictationId = (): string => (
  globalThis.crypto?.randomUUID?.()
  ?? `show-voice-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`
);

const cancelledError = (): VoiceTranscriptionError => new VoiceTranscriptionError('cancelled');

/** A capture that ended because its graph never rendered a frame (`VoiceTranscriptionError`
 *  'empty' from the recorder): it holds silence, not speech. */
const capturedNothing = (error: unknown): boolean => (
  error instanceof VoiceTranscriptionError && error.code === 'empty'
);

/**
 * One Show Page dictation backed by the exact recording, realtime, segmentation,
 * and HTTP fallback primitives used by Chat. Segment duration is internal; the
 * user-facing recording has no duration or aggregate-size limit.
 */
export class ShowPageVoiceDictation {
  private readonly input: ShowPageVoiceDictationInput;
  private readonly dependencies: Required<ShowPageVoiceDictationDependencies>;
  private readonly abortController = new AbortController();
  /** Aborted when capture stops: segments sent while recording stop retrying on their own, and
   *  finalization takes them over within its bounded window. */
  private readonly captureRetries = new AbortController();
  /** When the take's automatic attempts end (epoch ms), set when capture stops. */
  private settleBy: number | undefined;
  private readonly dictationId: string;
  private readonly segments: PendingVoiceSegment[] = [];
  private readonly queue: VoiceQueue;
  /** Settles once per dictation; every request reads the same context. */
  private readonly context: Promise<VoiceContext>;
  private cleanupContext: { before: string; after: string };
  private captureClaim: VoiceCaptureClaim | null = null;
  private pipeline: VoicePipeline | null = null;
  private realtime: VoiceRealtime | null = null;
  private realtimeState: 'connecting' | 'active' | 'failed' = 'connecting';
  private stream: MediaStream | null = null;
  private captureError: unknown;
  /** How much of what the microphone heard was loud enough to be speech. */
  private readonly activity = createVoiceActivity();
  /** A live preview carried words: the take held speech, whatever the levels say. */
  private heardWords = false;
  private startedAt: number | undefined;
  private stopped = false;
  private settled = false;
  private resolveDone!: (text: string) => void;
  private rejectDone!: (error: unknown) => void;
  readonly done: Promise<string>;

  constructor(
    input: ShowPageVoiceDictationInput,
    dependencies: ShowPageVoiceDictationDependencies = {},
  ) {
    this.input = input;
    this.cleanupContext = { before: input.before, after: input.after };
    this.dependencies = {
      getUserMedia: dependencies.getUserMedia
        ?? (() => navigator.mediaDevices.getUserMedia({ audio: true })),
      createPipeline: dependencies.createPipeline
        ?? ((options) => new VoiceRecordingPipeline(options)),
      createRealtime: dependencies.createRealtime
        ?? ((options) => new VoiceRealtimeSession(options)),
      createQueue: dependencies.createQueue
        ?? ((options) => new VoiceTranscriptionQueue(options)),
      finalize: dependencies.finalize ?? finalizeVoiceDictation,
      newDictationId: dependencies.newDictationId ?? newDictationId,
    };
    this.dictationId = this.dependencies.newDictationId();
    this.context = resolveVoiceContext(input.context);
    this.queue = this.dependencies.createQueue({
      concurrency: VOICE_TRANSCRIPTION_CONCURRENCY,
      signal: this.abortController.signal,
      retrySignal: this.captureRetries.signal,
      dictationId: this.dictationId,
      context: this.context,
    });
    this.done = new Promise<string>((resolve, reject) => {
      this.resolveDone = resolve;
      this.rejectDone = reject;
    });
    void this.done.catch(() => undefined);
  }

  async start(): Promise<void> {
    if (this.pipeline || this.stream) throw new Error('show page voice dictation already started');
    const captureClaim = this.input.captureClaim ?? claimVoiceCapture(() => {
      try {
        if (this.pipeline) this.pipeline.finish();
        else this.abortController.abort();
      } catch {
        this.abortController.abort();
        this.pipeline?.abort();
        this.stream?.getTracks().forEach((track) => track.stop());
      }
    });
    this.captureClaim = captureClaim;
    try {
      await captureClaim.ready;
      const stream = await this.dependencies.getUserMedia();
      if (this.abortController.signal.aborted || !captureClaim.isCurrent()) {
        stream.getTracks().forEach((track) => track.stop());
        throw cancelledError();
      }
      this.stream = stream;
      const realtime = this.dependencies.createRealtime({
        before: this.input.before,
        after: this.input.after,
        context: this.context,
        signal: this.abortController.signal,
        onPreview: (preview) => {
          if (`${preview.text}${preview.stash}`.trim()) this.heardWords = true;
          this.input.onPreview?.(`${preview.text}${preview.stash}`.trim());
          this.input.onPreviewParts?.(preview);
        },
        onError: () => {
          if (this.abortController.signal.aborted) return;
          this.realtimeState = 'failed';
          this.activateHttpFallback();
        },
      });
      this.realtime = realtime;
      void realtime.start().then(() => {
        if (!this.abortController.signal.aborted) this.realtimeState = 'active';
      }).catch(() => {
        if (this.abortController.signal.aborted) return;
        this.realtimeState = 'failed';
        this.activateHttpFallback();
      });

      const pipeline = this.dependencies.createPipeline({
        stream,
        segmentMs: VOICE_SEGMENT_MS,
        maxFileBytes: this.input.maxFileBytes,
        onSegment: (blob, metadata) => this.queueSegment(
          blob,
          metadata.durationMs,
          metadata.overlapMs,
          metadata.final,
        ),
        onPcm: (samples) => {
          // What would be transcribed, its flushed tail included, is what the gate judges.
          this.activity.observePcm(samples);
          if (this.realtimeState !== 'failed') realtime.sendPcm(samples);
        },
        ...(this.input.onLevel ? { onLevel: this.input.onLevel } : {}),
        onError: (error) => {
          this.captureError = error;
          this.realtimeState = 'failed';
          realtime.abort();
          this.activateHttpFallback();
        },
        onStopped: (reason) => {
          this.stopped = true;
          this.settleBy = Date.now() + VOICE_SETTLE_RETRY_WINDOW_MS;
          this.captureRetries.abort();
          this.releaseCaptureClaim();
          if (reason === 'abort') {
            this.reject(cancelledError());
            return;
          }
          void this.finalize().then(
            (text) => this.resolve(text),
            (error) => this.reject(error),
          );
        },
      });
      this.pipeline = pipeline;
      const active = await pipeline.start();
      if (!active) return;
      this.startedAt = Date.now();
      if (!captureClaim.isCurrent()) {
        pipeline.finish();
      }
    } catch (error) {
      this.abortController.abort();
      this.realtime?.abort();
      this.stream?.getTracks().forEach((track) => track.stop());
      this.stream = null;
      this.releaseCaptureClaim();
      throw error;
    }
  }

  finish(): void {
    this.pipeline?.finish();
  }

  abort(): void {
    if (this.abortController.signal.aborted) return;
    this.abortController.abort();
    this.realtime?.abort();
    this.pipeline?.abort();
    this.stream?.getTracks().forEach((track) => track.stop());
    this.releaseCaptureClaim();
    this.reject(cancelledError());
  }

  /** Whether the take holds audio that can be transcribed again: it ended, was not discarded,
   *  and recorded something. A capture that broke midway keeps what it recorded until then. */
  canRetry(): boolean {
    return this.stopped
      && !this.abortController.signal.aborted
      && !capturedNothing(this.captureError)
      && !this.isSilent()
      && this.segments.some((segment) => segment.blob !== null);
  }

  /** Whether the microphone heard enough sound for speech: an empty transcript of such a take is
   *  a recognition that failed, not a silence, and the take is kept. */
  voiced(): boolean {
    return this.heardWords || this.activity.voiced();
  }

  /** The silence gate: no live words, and no moment loud enough for speech. */
  private isSilent(): boolean {
    return !this.heardWords && this.activity.silent();
  }

  /** Sends the kept audio again through whichever path works now (cloud, then the local relay):
   *  the segments that have no receipt yet, then the last one. */
  async retry(context: { before: string; after: string }): Promise<string> {
    if (!this.canRetry()) {
      throw this.captureError ?? cancelledError();
    }
    this.cleanupContext = context;
    this.settleBy = Date.now() + VOICE_SETTLE_RETRY_WINDOW_MS;
    return this.finalizeHttp();
  }

  private queueSegment(
    blob: Blob,
    durationMs: number,
    overlapMs?: number,
    final = false,
  ): void {
    const segment: PendingVoiceSegment = {
      blob,
      sequence: this.segments.length,
      final,
      durationMs,
      overlapMs,
      task: Promise.resolve(),
    };
    this.segments.push(segment);
    if (this.realtimeState !== 'failed' || final) return;
    segment.transcriptionStarted = true;
    segment.task = this.queue.enqueue(segment);
  }

  private activateHttpFallback(): void {
    for (const segment of this.segments) {
      if (segment.final || segment.transcriptionStarted || !segment.blob) continue;
      segment.transcriptionStarted = true;
      segment.task = this.queue.enqueue(segment);
    }
  }

  private ensureFinalSegment(): void {
    if (this.segments.at(-1)?.final) return;
    this.segments.push({
      blob: null,
      sequence: this.segments.length,
      final: true,
      durationMs: 0,
      task: Promise.resolve(),
    });
  }

  private async finalize(): Promise<string> {
    if (!this.segments.length) {
      throw this.captureError ?? new VoiceTranscriptionError('empty');
    }
    // A graph that never rendered a frame recorded silence: there is nothing to keep.
    if (capturedNothing(this.captureError)) throw this.captureError;
    // Neither loud enough nor heard as words: no speech. Recognition is not asked, since on
    // near-silence it can return its hotwords as text (voiceActivity, the silence gate).
    if (this.isSilent()) {
      reportVoiceSilenceGate(this.activity, this.startedAt === undefined ? undefined : Date.now() - this.startedAt);
      this.realtime?.abort();
      throw new VoiceTranscriptionError('empty');
    }
    this.ensureFinalSegment();
    if (this.realtime && this.realtimeState !== 'failed' && this.captureError === undefined) {
      try {
        const result = await this.realtime.finish();
        if (!result.text.trim()) throw new VoiceTranscriptionError('empty');
        return result.text;
      } catch {
        if (this.abortController.signal.aborted) throw cancelledError();
        this.realtimeState = 'failed';
        // The wait for the realtime result is not the HTTP path's: it gets its own window.
        this.settleBy = Date.now() + VOICE_SETTLE_RETRY_WINDOW_MS;
        this.activateHttpFallback();
      }
    }
    return this.finalizeHttp();
  }

  /** Transcribes the kept audio over HTTP. A capture error does not void what was recorded
   *  before it: those segments are transcribed like any others. */
  private async finalizeHttp(): Promise<string> {
    this.activateHttpFallback();
    await Promise.all(this.segments.map((segment) => segment.task));
    if (this.abortController.signal.aborted) throw cancelledError();
    const result = await this.dependencies.finalize(this.segments, {
      dictationId: this.dictationId,
      before: this.cleanupContext.before,
      after: this.cleanupContext.after,
    }, {
      signal: this.abortController.signal,
      context: this.context,
      retryUntil: this.settleBy ?? Date.now() + VOICE_SETTLE_RETRY_WINDOW_MS,
    });
    return result.text;
  }

  private resolve(text: string): void {
    if (this.settled) return;
    this.settled = true;
    this.resolveDone(text);
  }

  private reject(error: unknown): void {
    if (this.settled) return;
    this.settled = true;
    this.rejectDone(error);
  }

  private releaseCaptureClaim(): void {
    this.captureClaim?.release();
    this.captureClaim = null;
  }
}
