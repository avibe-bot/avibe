type VoiceRecorderLike = {
  state: RecordingState;
  mimeType: string;
  ondataavailable: ((event: { data: Blob }) => void) | null;
  onstop: (() => void) | null;
  start: (timeslice?: number) => void;
  stop: () => void;
};

type VoiceRecorderHandle = {
  recorder: VoiceRecorderLike;
  chunks: Blob[];
  index: number;
};

export type VoiceRecordingStopReason = 'finish' | 'abort';

export type VoiceRecordingPipelineOptions = {
  stream: MediaStream;
  mimeType?: string;
  audioBitsPerSecond: number;
  segmentMs: number;
  timesliceMs?: number;
  onSegment: (blob: Blob) => void;
  onStopped: (reason: VoiceRecordingStopReason) => void;
  onError?: (error: unknown) => void;
  createRecorder?: (stream: MediaStream, options: MediaRecorderOptions) => VoiceRecorderLike;
};

const defaultRecorderFactory = (
  stream: MediaStream,
  options: MediaRecorderOptions,
): VoiceRecorderLike => new MediaRecorder(stream, options) as unknown as VoiceRecorderLike;

export const deleteMapValueIfCurrent = <Key, Value>(
  map: Map<Key, Value>,
  key: Key,
  value: Value,
): boolean => {
  if (map.get(key) !== value) return false;
  return map.delete(key);
};

/**
 * Produces independently decodable audio files without pausing capture between
 * them. At each boundary the next MediaRecorder starts before the previous one
 * stops, and terminal completion waits for every overlapping recorder callback.
 */
export class VoiceRecordingPipeline {
  private readonly options: VoiceRecordingPipelineOptions;
  private readonly createRecorder: NonNullable<VoiceRecordingPipelineOptions['createRecorder']>;
  private readonly handles = new Set<VoiceRecorderHandle>();
  private readonly completedSegments = new Map<number, Blob | null>();
  private active: VoiceRecorderHandle | null = null;
  private segmentTimer: ReturnType<typeof setTimeout> | null = null;
  private nextRecorderIndex = 0;
  private nextSegmentIndex = 0;
  private stopping: VoiceRecordingStopReason | null = null;
  private stopped = false;

  constructor(options: VoiceRecordingPipelineOptions) {
    this.options = options;
    this.createRecorder = options.createRecorder ?? defaultRecorderFactory;
  }

  start(): void {
    if (this.active || this.stopped) throw new Error('voice recording pipeline already started');
    const handle = this.createHandle();
    try {
      handle.recorder.start(this.options.timesliceMs ?? 1000);
    } catch (error) {
      this.handles.delete(handle);
      this.stopStream();
      this.stopped = true;
      throw error;
    }
    this.active = handle;
    this.scheduleRotation(handle);
  }

  finish(): void {
    this.stop('finish');
  }

  abort(): void {
    this.stop('abort');
  }

  private createHandle(): VoiceRecorderHandle {
    const recorder = this.createRecorder(this.options.stream, {
      ...(this.options.mimeType ? { mimeType: this.options.mimeType } : {}),
      audioBitsPerSecond: this.options.audioBitsPerSecond,
    });
    const handle: VoiceRecorderHandle = {
      recorder,
      chunks: [],
      index: this.nextRecorderIndex++,
    };
    this.handles.add(handle);
    recorder.ondataavailable = (event) => {
      if (event.data.size) handle.chunks.push(event.data);
    };
    recorder.onstop = () => this.handleRecorderStopped(handle);
    return handle;
  }

  private scheduleRotation(handle: VoiceRecorderHandle): void {
    this.clearSegmentTimer();
    this.segmentTimer = globalThis.setTimeout(
      () => this.rotate(handle),
      this.options.segmentMs,
    );
  }

  private rotate(current: VoiceRecorderHandle): void {
    if (
      this.stopping
      || this.active !== current
      || current.recorder.state !== 'recording'
    ) {
      return;
    }

    let next: VoiceRecorderHandle | null = null;
    try {
      next = this.createHandle();
      // The order is intentional: continuous input matters more than a few
      // milliseconds of overlap at a segment boundary.
      next.recorder.start(this.options.timesliceMs ?? 1000);
    } catch (error) {
      if (next) this.handles.delete(next);
      this.options.onError?.(error);
      this.stopping = 'finish';
      this.stopRecorder(current);
      return;
    }

    this.active = next;
    this.scheduleRotation(next);
    this.stopRecorder(current);
  }

  private stop(reason: VoiceRecordingStopReason): void {
    if (this.stopping || this.stopped) return;
    this.stopping = reason;
    this.clearSegmentTimer();
    if (!this.active) {
      this.complete();
      return;
    }
    this.stopRecorder(this.active);
  }

  private stopRecorder(handle: VoiceRecorderHandle): void {
    if (handle.recorder.state === 'recording' || handle.recorder.state === 'paused') {
      try {
        handle.recorder.stop();
        return;
      } catch (error) {
        this.options.onError?.(error);
      }
    }
    this.handleRecorderStopped(handle);
  }

  private handleRecorderStopped(handle: VoiceRecorderHandle): void {
    if (!this.handles.delete(handle)) return;

    const blob = new Blob(handle.chunks, {
      type: handle.recorder.mimeType || this.options.mimeType || 'audio/webm',
    });
    this.completedSegments.set(
      handle.index,
      this.stopping === 'abort' || !blob.size ? null : blob,
    );
    this.flushCompletedSegments();

    if (this.stopping && this.handles.size === 0) this.complete();
  }

  private flushCompletedSegments(): void {
    while (this.completedSegments.has(this.nextSegmentIndex)) {
      const blob = this.completedSegments.get(this.nextSegmentIndex);
      this.completedSegments.delete(this.nextSegmentIndex);
      this.nextSegmentIndex += 1;
      if (blob) this.options.onSegment(blob);
    }
  }

  private complete(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.clearSegmentTimer();
    this.stopStream();
    this.options.onStopped(this.stopping ?? 'finish');
  }

  private clearSegmentTimer(): void {
    if (this.segmentTimer != null) globalThis.clearTimeout(this.segmentTimer);
    this.segmentTimer = null;
  }

  private stopStream(): void {
    this.options.stream.getTracks().forEach((track) => track.stop());
  }
}
