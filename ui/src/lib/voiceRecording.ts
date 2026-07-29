const PCM_SAMPLE_RATE = 16_000;
const PCM_WORKLET_NAME = 'avibe-voice-pcm-capture';
const PCM_DELIVERY_MS = 250;

export const voicePcmDeliverySamples = (
  sampleRate: number,
  segmentSamples: number,
): number => Math.max(
  1,
  Math.min(segmentSamples, Math.round(sampleRate * PCM_DELIVERY_MS / 1000)),
);

const PCM_WORKLET_SOURCE = `
class AvibeVoicePcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const processorOptions = options.processorOptions || {};
    this.targetSampleRate = processorOptions.targetSampleRate || 16000;
    this.chunkSamples = processorOptions.chunkSamples || this.targetSampleRate;
    this.output = new Int16Array(this.chunkSamples);
    this.outputOffset = 0;
    this.phase = 0;
    this.sum = 0;
    this.sumCount = 0;
    this.previousSample = 0;
    this.hasPreviousSample = false;
    this.nextOutputPosition = 0;
    this.totalInputSamples = 0;
    this.totalOutputSamples = 0;
    this.stopped = false;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'stop') this.finish();
    };
  }

  emitSample(sample) {
    const normalized = Math.max(-1, Math.min(1, sample));
    this.output[this.outputOffset++] = normalized < 0
      ? Math.round(normalized * 32768)
      : Math.round(normalized * 32767);
    this.totalOutputSamples += 1;
    if (this.outputOffset === this.output.length) this.flush();
  }

  flush() {
    if (!this.outputOffset) return;
    const samples = this.outputOffset === this.output.length
      ? this.output
      : this.output.slice(0, this.outputOffset);
    this.port.postMessage({ type: 'samples', samples: samples.buffer }, [samples.buffer]);
    this.output = new Int16Array(this.chunkSamples);
    this.outputOffset = 0;
  }

  finish() {
    if (this.stopped) return;
    if (this.targetSampleRate <= sampleRate && this.sumCount) {
      this.emitSample(this.sum / this.sumCount);
      this.sum = 0;
      this.sumCount = 0;
    } else if (this.targetSampleRate > sampleRate && this.hasPreviousSample) {
      const expectedOutputSamples = Math.round(
        this.totalInputSamples * this.targetSampleRate / sampleRate,
      );
      while (this.totalOutputSamples < expectedOutputSamples) {
        this.emitSample(this.previousSample);
      }
    }
    this.flush();
    this.stopped = true;
    this.port.postMessage({ type: 'stopped' });
  }

  processDownsample(sample) {
    this.sum += sample;
    this.sumCount += 1;
    this.phase += this.targetSampleRate;
    if (this.phase >= sampleRate) {
      this.phase -= sampleRate;
      this.emitSample(this.sum / this.sumCount);
      this.sum = 0;
      this.sumCount = 0;
    }
  }

  processUpsample(sample) {
    const currentPosition = this.totalInputSamples;
    if (!this.hasPreviousSample) {
      this.previousSample = sample;
      this.hasPreviousSample = true;
      this.emitSample(sample);
      this.nextOutputPosition = sampleRate / this.targetSampleRate;
    } else {
      const previousPosition = currentPosition - 1;
      while (this.nextOutputPosition <= currentPosition) {
        const fraction = Math.max(
          0,
          Math.min(1, this.nextOutputPosition - previousPosition),
        );
        this.emitSample(
          this.previousSample + (sample - this.previousSample) * fraction,
        );
        this.nextOutputPosition += sampleRate / this.targetSampleRate;
      }
      this.previousSample = sample;
    }
    this.totalInputSamples += 1;
  }

  process(inputs) {
    if (this.stopped) return false;
    const channels = inputs[0];
    if (!channels || !channels.length) return true;
    const frameCount = channels[0].length;
    for (let frame = 0; frame < frameCount; frame += 1) {
      let sample = 0;
      for (let channel = 0; channel < channels.length; channel += 1) {
        sample += channels[channel][frame] || 0;
      }
      const monoSample = sample / channels.length;
      if (this.targetSampleRate <= sampleRate) this.processDownsample(monoSample);
      else this.processUpsample(monoSample);
    }
    return true;
  }
}

registerProcessor('${PCM_WORKLET_NAME}', AvibeVoicePcmCapture);
`;

export const voicePcmWorkletSource = PCM_WORKLET_SOURCE;

type VoicePcmCaptureHandlers = {
  onSamples: (samples: Int16Array<ArrayBuffer>) => void;
  onStopped: () => void;
  onError: (error: unknown) => void;
};

type VoicePcmCapture = {
  start: () => Promise<void>;
  stop: () => void;
};

type VoicePcmCaptureFactory = (
  stream: MediaStream,
  sampleRate: number,
  segmentSamples: number,
  handlers: VoicePcmCaptureHandlers,
) => VoicePcmCapture;

class AudioWorkletPcmCapture implements VoicePcmCapture {
  private readonly stream: MediaStream;
  private readonly sampleRate: number;
  private readonly segmentSamples: number;
  private readonly handlers: VoicePcmCaptureHandlers;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private node: AudioWorkletNode | null = null;
  private stopping = false;
  private stopped = false;

  constructor(
    stream: MediaStream,
    sampleRate: number,
    segmentSamples: number,
    handlers: VoicePcmCaptureHandlers,
  ) {
    this.stream = stream;
    this.sampleRate = sampleRate;
    this.segmentSamples = segmentSamples;
    this.handlers = handlers;
  }

  async start(): Promise<void> {
    const context = new AudioContext();
    this.context = context;
    this.stream.getTracks().forEach((track) => {
      track.addEventListener('ended', this.handleTrackEnded, { once: true });
    });
    if (this.stream.getTracks().some((track) => track.readyState === 'ended')) {
      this.stop();
    }
    let moduleUrl: string | null = null;
    try {
      moduleUrl = URL.createObjectURL(
        new Blob([PCM_WORKLET_SOURCE], { type: 'text/javascript' }),
      );
      await context.audioWorklet.addModule(moduleUrl);
      if (this.stopping) {
        await context.close();
        this.finish();
        return;
      }

      const node = new AudioWorkletNode(context, PCM_WORKLET_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: {
          targetSampleRate: this.sampleRate,
          // File-sized segments are assembled on the main thread. Deliver
          // smaller chunks so a processor failure cannot erase a whole segment.
          chunkSamples: voicePcmDeliverySamples(this.sampleRate, this.segmentSamples),
        },
      });
      node.port.onmessage = (event: MessageEvent<{
        type?: string;
        samples?: ArrayBuffer;
      }>) => {
        if (event.data.type === 'samples' && event.data.samples) {
          this.handlers.onSamples(new Int16Array(event.data.samples));
        } else if (event.data.type === 'stopped') {
          this.finish();
        }
      };
      node.onprocessorerror = () => {
        this.handlers.onError(new Error('voice PCM processor stopped unexpectedly'));
        this.finish();
      };
      this.node = node;
      this.source = context.createMediaStreamSource(this.stream);
      this.source.connect(node);
      node.connect(context.destination);
      await context.resume();
    } catch (error) {
      this.detachNodes();
      this.context = null;
      if (context.state !== 'closed') await context.close().catch(() => undefined);
      throw error;
    } finally {
      if (moduleUrl) URL.revokeObjectURL(moduleUrl);
    }
  }

  stop(): void {
    if (this.stopping || this.stopped) return;
    this.stopping = true;
    if (!this.node) return;
    this.node.port.postMessage({ type: 'stop' });
  }

  private readonly handleTrackEnded = (): void => {
    this.stop();
  };

  private finish(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.detachNodes();
    const context = this.context;
    this.context = null;
    if (context && context.state !== 'closed') void context.close().catch(() => undefined);
    this.handlers.onStopped();
  }

  private detachNodes(): void {
    this.stream.getTracks().forEach((track) => {
      track.removeEventListener('ended', this.handleTrackEnded);
    });
    this.source?.disconnect();
    this.node?.disconnect();
    this.node?.port.close();
    this.source = null;
    this.node = null;
  }
}

const defaultCaptureFactory: VoicePcmCaptureFactory = (
  stream,
  sampleRate,
  segmentSamples,
  handlers,
) => new AudioWorkletPcmCapture(stream, sampleRate, segmentSamples, handlers);

export const deleteMapValueIfCurrent = <Key, Value>(
  map: Map<Key, Value>,
  key: Key,
  value: Value,
): boolean => {
  if (map.get(key) !== value) return false;
  return map.delete(key);
};

export const isVoiceControlDisabled = (
  disabled: boolean,
  recording: boolean,
  transcribing: boolean,
  retained = false,
): boolean => transcribing || (!recording && (disabled || retained));

export type VoiceRecordingStopReason = 'finish' | 'abort';

export type VoiceRecordingSegmentMetadata = {
  durationMs: number;
};

export type VoiceRecordingStopMetadata = {
  requestedAt: number;
};

export type VoiceRecordingStoppedMetadata = {
  pendingSegmentCount: number;
};

export type VoiceRecordingPipelineOptions = {
  stream: MediaStream;
  segmentMs: number;
  onSegment: (blob: Blob, metadata: VoiceRecordingSegmentMetadata) => void;
  onStopRequested?: (
    reason: VoiceRecordingStopReason,
    metadata: VoiceRecordingStopMetadata,
  ) => void;
  onStopped: (
    reason: VoiceRecordingStopReason,
    metadata: VoiceRecordingStoppedMetadata,
  ) => void;
  onError?: (error: unknown) => void;
  sampleRate?: number;
  createCapture?: VoicePcmCaptureFactory;
};

const wavHeader = (
  sampleRate: number,
  sampleCount: number,
): Uint8Array<ArrayBuffer> => {
  const header = new Uint8Array(44);
  const view = new DataView(header.buffer);
  const writeAscii = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1) {
      view.setUint8(offset + index, value.charCodeAt(index));
    }
  };
  const dataBytes = sampleCount * 2;
  writeAscii(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeAscii(8, 'WAVE');
  writeAscii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, 'data');
  view.setUint32(40, dataBytes, true);
  return header;
};

const wavBlob = (
  sampleRate: number,
  sampleCount: number,
  chunks: Int16Array<ArrayBuffer>[],
): Blob => new Blob(
  [wavHeader(sampleRate, sampleCount), ...chunks],
  { type: 'audio/wav' },
);

/**
 * Captures mono PCM on the audio rendering thread, then frames it by sample
 * count. Even if the main thread is blocked, queued PCM chunks are converted
 * into provider-safe files instead of one oversized MediaRecorder blob.
 */
export class VoiceRecordingPipeline {
  private readonly options: VoiceRecordingPipelineOptions;
  private readonly sampleRate: number;
  private readonly segmentSamples: number;
  private readonly capture: VoicePcmCapture;
  private segmentChunks: Int16Array<ArrayBuffer>[] = [];
  private segmentSampleCount = 0;
  private visibilityDocument: Document | null = null;
  private started = false;
  private stopping: VoiceRecordingStopReason | null = null;
  private stopped = false;
  private emittedSegmentCount = 0;
  private segmentCountAtStop: number | null = null;

  constructor(options: VoiceRecordingPipelineOptions) {
    this.options = options;
    this.sampleRate = options.sampleRate ?? PCM_SAMPLE_RATE;
    this.segmentSamples = Math.max(
      1,
      Math.round(this.sampleRate * options.segmentMs / 1000),
    );
    const createCapture = options.createCapture ?? defaultCaptureFactory;
    this.capture = createCapture(options.stream, this.sampleRate, this.segmentSamples, {
      onSamples: (samples) => this.handleSamples(samples),
      onStopped: () => this.handleCaptureStopped(),
      onError: (error) => this.handleCaptureError(error),
    });
  }

  async start(): Promise<boolean> {
    if (this.started || this.stopped) {
      throw new Error('voice recording pipeline already started');
    }
    this.started = true;
    // Bind before asynchronous capture setup so a tab switch during addModule
    // or resume cannot be missed. A hidden document immediately enters finish.
    this.bindVisibilityStop();
    try {
      await this.capture.start();
    } catch (error) {
      if (this.stopped) return false;
      if (this.stopping) {
        this.complete(this.pendingSegmentCount());
        return false;
      }
      this.unbindVisibilityStop();
      this.stopStream();
      this.stopped = true;
      throw error;
    }
    return !this.stopped && !this.stopping;
  }

  finish(): void {
    this.stop('finish');
  }

  abort(): void {
    this.stop('abort');
  }

  private stop(reason: VoiceRecordingStopReason): void {
    if (!this.requestStop(reason)) return;
    this.capture.stop();
  }

  private requestStop(reason: VoiceRecordingStopReason): boolean {
    if (this.stopping || this.stopped) return false;
    this.stopping = reason;
    this.segmentCountAtStop = this.emittedSegmentCount;
    this.options.onStopRequested?.(reason, { requestedAt: Date.now() });
    return true;
  }

  private handleSamples(samples: Int16Array<ArrayBuffer>): void {
    if (this.stopped || this.stopping === 'abort') return;
    let offset = 0;
    while (offset < samples.length) {
      const available = this.segmentSamples - this.segmentSampleCount;
      const take = Math.min(available, samples.length - offset);
      this.segmentChunks.push(samples.subarray(offset, offset + take));
      this.segmentSampleCount += take;
      offset += take;
      if (this.segmentSampleCount === this.segmentSamples) {
        this.emitSegment();
      }
    }
  }

  private emitSegment(): void {
    if (!this.segmentSampleCount) return;
    const sampleCount = this.segmentSampleCount;
    const chunks = this.segmentChunks;
    this.segmentChunks = [];
    this.segmentSampleCount = 0;
    this.emittedSegmentCount += 1;
    this.options.onSegment(
      wavBlob(this.sampleRate, sampleCount, chunks),
      { durationMs: Math.round(sampleCount * 1000 / this.sampleRate) },
    );
  }

  private handleCaptureError(error: unknown): void {
    this.options.onError?.(error);
    this.requestStop('finish');
  }

  private handleCaptureStopped(): void {
    if (this.stopped) return;
    this.requestStop('finish');
    if (this.stopping === 'finish') this.emitSegment();
    else {
      this.segmentChunks = [];
      this.segmentSampleCount = 0;
    }
    this.complete(this.pendingSegmentCount());
  }

  private pendingSegmentCount(): number {
    if (this.stopping !== 'finish') return 0;
    return this.emittedSegmentCount - (this.segmentCountAtStop ?? this.emittedSegmentCount);
  }

  private complete(pendingSegmentCount: number): void {
    if (this.stopped) return;
    this.stopped = true;
    this.unbindVisibilityStop();
    this.stopStream();
    this.options.onStopped(
      this.stopping ?? 'finish',
      { pendingSegmentCount },
    );
  }

  private bindVisibilityStop(): void {
    if (typeof document === 'undefined') return;
    this.visibilityDocument = document;
    document.addEventListener('visibilitychange', this.handleVisibilityChange);
    if (document.visibilityState === 'hidden') this.finish();
  }

  private unbindVisibilityStop(): void {
    this.visibilityDocument?.removeEventListener('visibilitychange', this.handleVisibilityChange);
    this.visibilityDocument = null;
  }

  private readonly handleVisibilityChange = (): void => {
    if (this.visibilityDocument?.visibilityState === 'hidden') this.finish();
  };

  private stopStream(): void {
    this.options.stream.getTracks().forEach((track) => track.stop());
  }
}
