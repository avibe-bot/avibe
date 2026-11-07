// A take recorded without Web Audio, for a page whose real-time audio has stalled
// (lib/voiceEngine): a MediaRecorder on the take's own microphone track, which the stall does not
// touch. There is no live transcript and no level on this path; each segment's recording is
// decoded to the pipeline's 16 kHz PCM once it is complete (an OfflineAudioContext does not render
// in real time), so it is segmented, gated for silence and transcribed like any other take. A
// recording that cannot be decoded is transcribed as it is (audio/mp4 or audio/webm), unjudged.

/** A recorder delivers its data this often, so a microphone that gives nothing shows soon. */
const RECORDER_TIMESLICE_MS = 500;
/** No data from the recorder in this long: the microphone gives no sound at all. */
export const VOICE_RECORDER_SILENT_MS = 1_500;
/** The formats asked for, in order: an iPhone records audio/mp4. */
const RECORDER_TYPES = ['audio/mp4', 'audio/webm', 'audio/ogg'] as const;

type RecorderConstructor = {
  new (stream: MediaStream, options?: MediaRecorderOptions): MediaRecorder;
  isTypeSupported?: (type: string) => boolean;
};

const recorderConstructor = (): RecorderConstructor | null => {
  const Recorder = (globalThis as { MediaRecorder?: RecorderConstructor }).MediaRecorder;
  return typeof Recorder === 'function' ? Recorder : null;
};

/** Whether this browser can record a microphone without Web Audio. */
export const canRecordWithoutWebAudio = (): boolean => recorderConstructor() !== null;

const recorderType = (Recorder: RecorderConstructor): string | undefined => RECORDER_TYPES.find((type) => {
  try {
    return Recorder.isTypeSupported?.(type) ?? false;
  } catch {
    return false;
  }
});

type OfflineContextConstructor = new (channels: number, length: number, sampleRate: number) => OfflineAudioContext;

/** A recording as mono PCM at `sampleRate`: decoded, resampled to the context's rate as the Web
 *  Audio spec requires, and mixed down. */
export async function decodeVoiceRecording(blob: Blob, sampleRate: number): Promise<Int16Array<ArrayBuffer>> {
  const Offline = (globalThis as { OfflineAudioContext?: OfflineContextConstructor }).OfflineAudioContext;
  if (typeof Offline !== 'function') throw new Error('OfflineAudioContext unavailable');
  const context = new Offline(1, 1, sampleRate);
  const buffer = await context.decodeAudioData(await blob.arrayBuffer());
  const channels = Array.from({ length: buffer.numberOfChannels }, (_, index) => buffer.getChannelData(index));
  const samples = new Int16Array(buffer.length);
  for (let frame = 0; frame < buffer.length; frame += 1) {
    let sum = 0;
    for (const channel of channels) sum += channel[frame] ?? 0;
    const sample = Math.max(-1, Math.min(1, channels.length ? sum / channels.length : 0));
    samples[frame] = sample < 0 ? Math.round(sample * 32_768) : Math.round(sample * 32_767);
  }
  return samples;
}

export type VoiceRecorderHandlers = {
  /** A finished segment, decoded: its PCM, in order. */
  onSamples: (samples: Int16Array<ArrayBuffer>) => void;
  /** A finished segment that could not be decoded, to be transcribed as it is; `last` when the
   *  recording has stopped and nothing follows it. */
  onFile: (blob: Blob, durationMs: number, last: boolean) => void;
  /** Whether the microphone is giving the recorder any data: false after
   *  `VOICE_RECORDER_SILENT_MS` without any, true again once some arrives. */
  onDelivering: (delivering: boolean) => void;
  /** What the recorder did, for the capture trace. */
  onData?: (bytes: number) => void;
  onSegment?: (decoded: boolean) => void;
  onError: (error: unknown) => void;
};

/** PCM chunks of this many samples go to the pipeline, as the worklet's do. */
const PCM_CHUNK_SAMPLES = 4_000;

/**
 * Records `stream` in segments of `segmentMs` (one recorder each, so each file stands alone and
 * is decoded with bounded memory), hands each segment over in order once complete, and says
 * whether any data is arriving at all.
 */
export class VoiceRecorderSegments {
  readonly type: string | undefined;
  private readonly Recorder: RecorderConstructor;
  private recorder: MediaRecorder | null = null;
  private rotateTimer: ReturnType<typeof setTimeout> | null = null;
  private silentTimer: ReturnType<typeof setTimeout> | null = null;
  private delivering: boolean | null = null;
  /** Segments are handed over in the order they were recorded. */
  private queue: Promise<void> = Promise.resolve();
  private stopping = false;
  private discarded = false;
  private settled: Promise<void> | null = null;
  private readonly stream: MediaStream;
  private readonly segmentMs: number;
  private readonly sampleRate: number;
  private readonly handlers: VoiceRecorderHandlers;

  constructor(stream: MediaStream, segmentMs: number, sampleRate: number, handlers: VoiceRecorderHandlers) {
    this.stream = stream;
    this.segmentMs = segmentMs;
    this.sampleRate = sampleRate;
    this.handlers = handlers;
    const Recorder = recorderConstructor();
    if (!Recorder) throw new Error('MediaRecorder unavailable');
    this.Recorder = Recorder;
    this.type = recorderType(Recorder);
  }

  start(): void {
    this.startSegment();
    // Once for the recording: a new segment does not put off saying the microphone gives nothing.
    this.watchDelivery();
  }

  private startSegment(): void {
    const recorder = new this.Recorder(this.stream, this.type ? { mimeType: this.type } : undefined);
    const chunks: Blob[] = [];
    this.recorder = recorder;
    const startedAt = Date.now();
    recorder.ondataavailable = (event: BlobEvent) => {
      if (!event.data?.size) return;
      chunks.push(event.data);
      this.handlers.onData?.(event.data.size);
      this.setDelivering(true);
    };
    recorder.onerror = (event: Event) => {
      this.handlers.onError((event as Event & { error?: unknown }).error ?? new Error('voice recorder failed'));
    };
    const ended = new Promise<void>((resolve) => {
      recorder.onstop = () => resolve();
    });
    recorder.start(RECORDER_TIMESLICE_MS);
    // The segment is handed over once its recorder has stopped and the ones before it are out.
    this.queue = this.queue.then(async () => {
      await ended;
      if (this.discarded) return;
      const last = this.stopping && this.recorder === recorder;
      await this.handOver(chunks, Date.now() - startedAt, recorder.mimeType || this.type, last);
    });
    if (!this.stopping) {
      this.rotateTimer = setTimeout(() => {
        this.rotateTimer = null;
        if (this.stopping) return;
        recorder.stop();
        this.startSegment();
      }, this.segmentMs);
    }
  }

  private async handOver(chunks: Blob[], durationMs: number, type: string | undefined, last: boolean): Promise<void> {
    if (!chunks.length) return;
    const blob = new Blob(chunks, type ? { type } : undefined);
    try {
      const samples = await decodeVoiceRecording(blob, this.sampleRate);
      if (this.discarded) return;
      this.handlers.onSegment?.(true);
      for (let offset = 0; offset < samples.length; offset += PCM_CHUNK_SAMPLES) {
        this.handlers.onSamples(samples.slice(offset, offset + PCM_CHUNK_SAMPLES));
      }
    } catch {
      if (this.discarded) return;
      // Not decodable here: the recording itself is transcribed.
      this.handlers.onSegment?.(false);
      this.handlers.onFile(blob, Math.max(0, Math.round(durationMs)), last);
    }
  }

  private watchDelivery(): void {
    if (this.silentTimer !== null) clearTimeout(this.silentTimer);
    if (this.delivering) return;
    this.silentTimer = setTimeout(() => {
      this.silentTimer = null;
      if (!this.stopping && this.delivering !== true) this.setDelivering(false);
    }, VOICE_RECORDER_SILENT_MS);
  }

  private setDelivering(delivering: boolean): void {
    if (this.delivering === delivering) return;
    this.delivering = delivering;
    this.handlers.onDelivering(delivering);
  }

  /** Stops recording and resolves once every segment has been handed over. */
  stop(): Promise<void> {
    if (this.settled) return this.settled;
    this.stopping = true;
    if (this.rotateTimer !== null) clearTimeout(this.rotateTimer);
    if (this.silentTimer !== null) clearTimeout(this.silentTimer);
    this.rotateTimer = null;
    this.silentTimer = null;
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    this.settled = this.queue;
    return this.settled;
  }

  /** Stops recording and drops what it recorded: the take went back to Web Audio, or was discarded. */
  discard(): void {
    this.discarded = true;
    void this.stop();
  }
}
