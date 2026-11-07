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
  VOICE_LEVEL_INTERVAL_MS,
  VoiceRecordingPipeline,
  type VoiceCaptureClaim,
  type VoiceCaptureState,
  type VoiceRecordingPipelineOptions,
} from './voiceRecording';
import { createVoiceActivity, reportVoiceSilenceGate, VOICE_SPEECH_RMS } from './voiceActivity';
import { VoiceCaptureTrace, type VoiceCaptureClass, type VoiceCaptureEntry } from './voiceCaptureTrace';
import { VoiceTakeKeeper, type VoiceTakeKeep } from './voiceTakeKeeper';
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
  /** Keep the take on this device until its words land (lib/voiceTakeStore): where they go. */
  keep?: VoiceTakeKeep;
  /** Whether live recognition is working: false once the realtime path failed, or produced no
   *  preview while the microphone heard speech for `VOICE_LIVE_SILENT_SPEECH_MS`. What is said
   *  is still recorded and transcribed when the take ends. */
  onLiveChange?: (live: boolean) => void;
  /** Where the take started, for the capture trace: a press-and-hold, or a Show Page (default). */
  entry?: VoiceCaptureEntry;
  /** How the take is recorded (lib/voiceRecording): through Web Audio, or without it (no live
   *  words, no level: its words come on release), or without it and getting no sound at all. */
  onCaptureState?: (state: VoiceCaptureState) => void;
};

/** Speech heard without one live preview for this long: the realtime path is taken as not
 *  working, and the take goes on over HTTP. */
const VOICE_LIVE_SILENT_SPEECH_MS = 4_000;

/** A take set aside (`setAside`) is kept when it held speech for at least this long; a shorter
 *  one is taken for a slip, and discarded. */
export const VOICE_SET_ASIDE_MIN_MS = 3_000;

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
  /** Part of the take was recorded in a form this browser could not decode: its sound is unknown. */
  private unjudgedAudio = false;
  private startedAt: number | undefined;
  /** When capture was asked to end (or ended), for the take's length. */
  private endedAt: number | undefined;
  /** The reader stopped waiting for this take's words (`setAside`): nothing more is transcribed,
   *  and once capture ends the take is kept for the pending voice items or discarded. */
  private setAsideRequested = false;
  /** Settles `setAside` for a capture that was still writing its last segment. */
  private setAsideDecided: ((kept: boolean) => void) | null = null;
  /** Speech heard since the last live preview (or the start), in ms. */
  private speechWithoutPreviewMs = 0;
  private live = true;
  private readonly keeper: VoiceTakeKeeper | null;
  /** What the take's capture did, from the microphone request on (lib/voiceCaptureTrace). */
  private readonly trace: VoiceCaptureTrace;
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
    this.trace = new VoiceCaptureTrace(input.entry ?? 'show_page', this.dictationId);
    this.keeper = input.keep ? new VoiceTakeKeeper(this.dictationId, input.keep) : null;
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
      const requestedAt = this.trace.mark();
      let stream: MediaStream;
      try {
        stream = await this.dependencies.getUserMedia();
      } catch (error) {
        // The microphone never opened: refused, missing, or held by something else.
        this.trace.micSettled(requestedAt, error);
        throw error;
      }
      this.trace.micSettled(requestedAt);
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
          this.speechWithoutPreviewMs = 0;
          if (`${preview.text}${preview.stash}`.trim()) this.heardWords = true;
          this.input.onPreview?.(`${preview.text}${preview.stash}`.trim());
          this.input.onPreviewParts?.(preview);
        },
        onError: () => {
          if (this.abortController.signal.aborted) return;
          this.realtimeFailed();
        },
      });
      this.realtime = realtime;
      this.keeper?.begin();
      void realtime.start().then(() => {
        if (!this.abortController.signal.aborted && this.realtimeState !== 'failed') this.realtimeState = 'active';
      }).catch(() => {
        if (this.abortController.signal.aborted) return;
        this.realtimeFailed();
      });

      const pipeline = this.dependencies.createPipeline({
        stream,
        trace: this.trace,
        segmentMs: VOICE_SEGMENT_MS,
        maxFileBytes: this.input.maxFileBytes,
        onSegment: (blob, metadata) => {
          if (metadata.unjudged) this.unjudgedAudio = true;
          this.queueSegment(blob, metadata.durationMs, metadata.overlapMs, metadata.final);
        },
        onPcm: (samples) => {
          // What would be transcribed, its flushed tail included, is what the gate judges.
          this.activity.observePcm(samples);
          if (this.realtimeState !== 'failed') realtime.sendPcm(samples);
        },
        onLevel: (rms) => {
          this.watchLiveness(rms);
          this.input.onLevel?.(rms);
        },
        onCaptureState: (state) => {
          // Without Web Audio there is no live PCM: the take's words come over HTTP on release.
          if (state !== 'live' && this.realtimeState !== 'failed') {
            this.realtime?.abort();
            this.realtimeFailed();
          }
          this.input.onCaptureState?.(state);
        },
        onError: (error) => {
          this.captureError = error;
          realtime.abort();
          this.realtimeFailed();
        },
        onStopped: (reason) => {
          this.stopped = true;
          this.endedAt ??= Date.now();
          this.settleBy = Date.now() + VOICE_SETTLE_RETRY_WINDOW_MS;
          this.captureRetries.abort();
          this.releaseCaptureClaim();
          if (this.setAsideRequested) {
            // Everything it recorded is in, its last audio too: kept, or discarded.
            this.decideSetAside();
            return;
          }
          if (reason === 'abort') {
            this.reject(cancelledError());
            return;
          }
          this.keeper?.pending();
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
      // Every take is logged once, however its start ended: refused, cancelled (released while
      // its microphone was granted), or failed. A pipeline that started has logged it already.
      this.trace.finish(error instanceof VoiceTranscriptionError && error.code === 'cancelled' ? 'cancelled' : 'failed');
      this.abortController.abort();
      this.realtime?.abort();
      this.stream?.getTracks().forEach((track) => track.stop());
      this.stream = null;
      this.releaseCaptureClaim();
      throw error;
    }
  }

  finish(): void {
    if (this.pipeline) this.endedAt ??= Date.now();
    this.pipeline?.finish();
  }

  /** This take's id, as its telemetry carries it. */
  get id(): string {
    return this.dictationId;
  }

  /** What the microphone delivered so far (lib/voiceCaptureTrace): no frames, muted by the
   *  system, silence, or sound; `no_microphone` before it opened. */
  captureClass(): VoiceCaptureClass {
    return this.trace.captureClass();
  }

  /** The take's words were inserted or sent: its kept copy is deleted. */
  landed(): void {
    this.keeper?.forget();
  }

  /**
   * The surface holding this take is gone (unmounted, navigated away). Nothing is sent from it,
   * but nothing is lost either: a take still recording finishes (what was heard is kept), one
   * still transcribing goes on, and the result stays on the device as a pending voice item.
   */
  detach(): void {
    if (this.abortController.signal.aborted) return;
    if (!this.stopped) this.pipeline?.finish();
    if (!this.keeper) {
      this.abort();
      return;
    }
    void this.done.then(() => undefined, () => undefined).finally(() => this.keeper?.release());
  }

  /** Discards the take: capture stops, nothing more is sent, and its kept copy is deleted. A take
   *  set aside is the pending voice items' already, and stays. */
  abort(): void {
    if (this.setAsideRequested) return;
    this.keeper?.forget();
    if (this.abortController.signal.aborted) return;
    this.abortController.abort();
    this.realtime?.abort();
    this.pipeline?.abort();
    this.stream?.getTracks().forEach((track) => track.stop());
    this.releaseCaptureClaim();
    this.reject(cancelledError());
  }

  /**
   * The reader stopped waiting for this take's words (the card's × while it is finalized): every
   * request and retry stops at once, and nothing lands from here. A take worth keeping (it passed
   * the silence gate and lasted `VOICE_SET_ASIDE_MIN_MS`) stays on the device for the pending
   * voice items, which leave it to the reader to retry or discard; anything else is discarded.
   * Resolves whether it was kept: at once, or, for a capture still writing its last segment, once
   * that last audio is in (it counts toward the silence gate like the rest).
   */
  setAside(): Promise<boolean> {
    if (this.abortController.signal.aborted) return Promise.resolve(false);
    if (!this.keeper || !this.pipeline || this.stopped) {
      if (!this.worthKeeping()) {
        this.abort();
        return Promise.resolve(false);
      }
      this.stopWaiting();
      this.keeper?.setAside();
      return Promise.resolve(true);
    }
    this.stopWaiting();
    const decided = new Promise<boolean>((resolve) => {
      this.setAsideDecided = resolve;
    });
    this.pipeline.finish();
    return decided;
  }

  /** Nothing more is asked of the network for this take, and nothing lands from it. */
  private stopWaiting(): void {
    this.setAsideRequested = true;
    this.abortController.abort();
    this.realtime?.abort();
    this.reject(cancelledError());
  }

  /** A take set aside while its capture was ending, judged once all of it is in. */
  private decideSetAside(): void {
    const kept = this.worthKeeping();
    if (kept) this.keeper?.setAside();
    else this.keeper?.forget();
    this.setAsideDecided?.(kept);
    this.setAsideDecided = null;
  }

  /** A take set aside is kept only when there is a place to keep it, it held speech, it recorded
   *  audio, and it is long enough not to be a slip. */
  private worthKeeping(): boolean {
    if (!this.keeper || this.startedAt === undefined) return false;
    if (capturedNothing(this.captureError) || this.isSilent()) return false;
    if (!this.segments.some((segment) => segment.blob !== null)) return false;
    return (this.endedAt ?? Date.now()) - this.startedAt >= VOICE_SET_ASIDE_MIN_MS;
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
  /** The take may hold speech: live words, a moment loud enough for it, or audio whose sound was
   *  never judged (a recording this browser could not decode). */
  voiced(): boolean {
    return this.heardWords || this.unjudgedAudio || this.activity.voiced();
  }

  /** The silence gate: no live words, no moment loud enough for speech, and nothing unjudged. */
  private isSilent(): boolean {
    return !this.heardWords && !this.unjudgedAudio && this.activity.silent();
  }

  /** Sends the kept audio again through whichever path works now (cloud, then the local relay):
   *  the segments that have no receipt yet, then the last one. */
  async retry(context: { before: string; after: string }): Promise<string> {
    if (!this.canRetry()) {
      throw this.captureError ?? cancelledError();
    }
    this.cleanupContext = context;
    this.settleBy = Date.now() + VOICE_SETTLE_RETRY_WINDOW_MS;
    try {
      const text = await this.finalizeHttp();
      this.keeper?.transcribed(text);
      return text;
    } catch (error) {
      if (!(error instanceof VoiceTranscriptionError && error.code === 'cancelled')) this.keeper?.pending();
      throw error;
    }
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
    this.keeper?.segment(segment, blob);
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
    const marker: PendingVoiceSegment = {
      blob: null,
      sequence: this.segments.length,
      final: true,
      durationMs: 0,
      task: Promise.resolve(),
    };
    this.segments.push(marker);
    this.keeper?.segment(marker, null);
  }

  /** The realtime path cannot carry this take: what was recorded goes over HTTP instead. */
  private realtimeFailed(): void {
    this.realtimeState = 'failed';
    this.activateHttpFallback();
    this.setLive(false);
  }

  /** Speech that produces no live preview for a while means the realtime path is not working
   *  (a socket that is open but stalled): the take stops waiting on it. */
  private watchLiveness(rms: number): void {
    if (this.realtimeState === 'failed' || this.stopped) return;
    if (!(rms >= VOICE_SPEECH_RMS)) return;
    this.speechWithoutPreviewMs += VOICE_LEVEL_INTERVAL_MS;
    if (this.speechWithoutPreviewMs < VOICE_LIVE_SILENT_SPEECH_MS) return;
    this.realtime?.abort();
    this.realtimeFailed();
  }

  private setLive(live: boolean): void {
    if (this.live === live) return;
    this.live = live;
    this.input.onLiveChange?.(live);
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
    this.keeper?.transcribed(text);
    this.resolveDone(text);
  }

  private reject(error: unknown): void {
    if (this.settled) return;
    this.settled = true;
    // A discarded take is forgotten by abort(), and a silent one has nothing to keep; one that
    // failed stays on the device.
    if (error instanceof VoiceTranscriptionError && error.code === 'empty' && !this.voiced()) this.keeper?.forget();
    else if (!(error instanceof VoiceTranscriptionError && error.code === 'cancelled')) this.keeper?.pending();
    this.rejectDone(error);
  }

  private releaseCaptureClaim(): void {
    this.captureClaim?.release();
    this.captureClaim = null;
  }
}
