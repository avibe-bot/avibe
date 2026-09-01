import {
  finalizeVoiceDictation,
  transcribeVoiceSegments,
  VOICE_SEGMENT_MS,
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
import {
  VoiceRealtimeSession,
  type VoiceRealtimeOptions,
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
};

export type ShowPageVoiceDictationDependencies = {
  getUserMedia?: () => Promise<MediaStream>;
  createPipeline?: (options: VoiceRecordingPipelineOptions) => VoicePipeline;
  createRealtime?: (options: VoiceRealtimeOptions) => VoiceRealtime;
  createQueue?: (options: {
    concurrency: number;
    signal: AbortSignal;
    dictationId: string;
  }) => VoiceQueue;
  finalize?: typeof finalizeVoiceDictation;
  transcribeSegments?: typeof transcribeVoiceSegments;
  newDictationId?: () => string;
};

const newDictationId = (): string => (
  globalThis.crypto?.randomUUID?.()
  ?? `show-voice-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`
);

const cancelledError = (): VoiceTranscriptionError => new VoiceTranscriptionError('cancelled');

/**
 * One Show Page dictation backed by the exact recording, realtime, segmentation,
 * and HTTP fallback primitives used by Chat. Segment duration is internal; the
 * user-facing recording has no duration or aggregate-size limit.
 */
export class ShowPageVoiceDictation {
  private readonly input: ShowPageVoiceDictationInput;
  private readonly dependencies: Required<ShowPageVoiceDictationDependencies>;
  private readonly abortController = new AbortController();
  private readonly dictationId: string;
  private readonly segments: PendingVoiceSegment[] = [];
  private readonly queue: VoiceQueue;
  private cleanupContext: { before: string; after: string };
  private captureClaim: VoiceCaptureClaim | null = null;
  private pipeline: VoicePipeline | null = null;
  private realtime: VoiceRealtime | null = null;
  private realtimeState: 'connecting' | 'active' | 'failed' = 'connecting';
  private stream: MediaStream | null = null;
  private captureError: unknown;
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
      transcribeSegments: dependencies.transcribeSegments ?? transcribeVoiceSegments,
      newDictationId: dependencies.newDictationId ?? newDictationId,
    };
    this.dictationId = this.dependencies.newDictationId();
    this.queue = this.dependencies.createQueue({
      concurrency: VOICE_TRANSCRIPTION_CONCURRENCY,
      signal: this.abortController.signal,
      dictationId: this.dictationId,
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
      const stream = await this.dependencies.getUserMedia();
      if (this.abortController.signal.aborted || !captureClaim.isCurrent()) {
        stream.getTracks().forEach((track) => track.stop());
        throw cancelledError();
      }
      this.stream = stream;
      const realtime = this.dependencies.createRealtime({
        before: this.input.before,
        after: this.input.after,
        signal: this.abortController.signal,
        onPreview: (preview) => this.input.onPreview?.(`${preview.text}${preview.stash}`.trim()),
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
          if (this.realtimeState !== 'failed') realtime.sendPcm(samples);
        },
        onError: (error) => {
          this.captureError = error;
          this.realtimeState = 'failed';
          realtime.abort();
          this.activateHttpFallback();
        },
        onStopped: (reason) => {
          this.stopped = true;
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

  canRetry(): boolean {
    return this.stopped
      && this.captureError === undefined
      && !this.abortController.signal.aborted;
  }

  async retry(context: { before: string; after: string }): Promise<string> {
    if (!this.canRetry()) {
      throw this.captureError ?? cancelledError();
    }
    this.cleanupContext = context;
    await this.dependencies.transcribeSegments(this.segments, {
      concurrency: VOICE_TRANSCRIPTION_CONCURRENCY,
      signal: this.abortController.signal,
      dictationId: this.dictationId,
    });
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
    if (!this.segments.length && this.captureError === undefined) {
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
        this.activateHttpFallback();
      }
    }
    return this.finalizeHttp();
  }

  private async finalizeHttp(): Promise<string> {
    this.activateHttpFallback();
    await Promise.all(this.segments.map((segment) => segment.task));
    if (this.abortController.signal.aborted) throw cancelledError();
    if (this.captureError !== undefined) throw this.captureError;
    const result = await this.dependencies.finalize(this.segments, {
      dictationId: this.dictationId,
      before: this.cleanupContext.before,
      after: this.cleanupContext.after,
    }, {
      signal: this.abortController.signal,
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
