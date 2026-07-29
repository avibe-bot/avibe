import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  deleteMapValueIfCurrent,
  isVoiceControlDisabled,
  voicePcmDeliverySamples,
  voicePcmWorkletSource,
  VoiceRecordingPipeline,
} from './voiceRecording';

type CaptureHandlers = {
  onSamples: (samples: Int16Array) => void;
  onStopped: () => void;
  onError: (error: unknown) => void;
};

class FakeCapture {
  private readonly handlers: CaptureHandlers;
  readonly start = vi.fn(async () => undefined);
  readonly stop = vi.fn();

  constructor(handlers: CaptureHandlers) {
    this.handlers = handlers;
  }

  emit(...samples: number[]): void {
    this.handlers.onSamples(Int16Array.from(samples));
  }

  settle(): void {
    this.handlers.onStopped();
  }

  fail(error: unknown): void {
    this.handlers.onError(error);
    this.handlers.onStopped();
  }
}

const setup = () => {
  const track = {
    stop: vi.fn(),
  };
  const stream = {
    getTracks: () => [track],
  } as unknown as MediaStream;
  const onSegment = vi.fn<(blob: Blob, metadata: { durationMs: number }) => void>();
  const onError = vi.fn();
  const onStopRequested = vi.fn();
  const onStopped = vi.fn();
  let capture: FakeCapture | null = null;
  const pipeline = new VoiceRecordingPipeline({
    stream,
    sampleRate: 4,
    segmentMs: 1000,
    onSegment,
    onError,
    onStopRequested,
    onStopped,
    createCapture: (_stream, sampleRate, segmentSamples, handlers) => {
      expect(sampleRate).toBe(4);
      expect(segmentSamples).toBe(4);
      capture = new FakeCapture(handlers);
      return capture;
    },
  });
  return {
    capture: () => capture!,
    onError,
    onSegment,
    onStopRequested,
    onStopped,
    pipeline,
    track,
  };
};

const wavDataBytes = async (blob: Blob): Promise<number> => {
  const buffer = await blob.arrayBuffer();
  return new DataView(buffer).getUint32(40, true);
};

const wavSamples = async (blob: Blob): Promise<number[]> => {
  const buffer = await blob.arrayBuffer();
  const view = new DataView(buffer);
  const samples: number[] = [];
  for (let offset = 44; offset < buffer.byteLength; offset += 2) {
    samples.push(view.getInt16(offset, true));
  }
  return samples;
};

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('VoiceRecordingPipeline', () => {
  it('frames delayed PCM delivery into provider-safe segments by sample count', async () => {
    const { capture, onSegment, pipeline } = setup();
    await pipeline.start();

    // This represents audio-render-thread messages delivered after a long main
    // thread stall. No emitted file may grow with the delayed wall time.
    capture().emit(1, 2, 3, 4, 5, 6, 7, 8, 9, 10);

    expect(onSegment).toHaveBeenCalledTimes(2);
    expect(onSegment.mock.calls.map(([, metadata]) => metadata.durationMs)).toEqual([
      1000,
      1000,
    ]);
    expect(await Promise.all(onSegment.mock.calls.map(([blob]) => wavDataBytes(blob)))).toEqual([
      8,
      8,
    ]);
    expect(await Promise.all(onSegment.mock.calls.map(([blob]) => wavSamples(blob)))).toEqual([
      [1, 2, 3, 4],
      [5, 6, 7, 8],
    ]);
    expect(onSegment.mock.calls.every(([blob]) => blob.type === 'audio/wav')).toBe(true);
  });

  it('flushes the final partial segment after the user stops', async () => {
    vi.setSystemTime(new Date('2026-07-29T17:00:00Z'));
    const {
      capture,
      onSegment,
      onStopRequested,
      onStopped,
      pipeline,
      track,
    } = setup();
    await pipeline.start();
    capture().emit(1, 2);

    pipeline.finish();

    expect(capture().stop).toHaveBeenCalledTimes(1);
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(onStopRequested).toHaveBeenCalledWith('finish', {
      requestedAt: Date.now(),
    });
    expect(onStopped).not.toHaveBeenCalled();

    capture().settle();

    expect(onSegment).toHaveBeenCalledTimes(1);
    expect(onSegment.mock.calls[0]?.[1]).toEqual({ durationMs: 500 });
    expect(await wavDataBytes(onSegment.mock.calls[0]![0])).toBe(4);
    expect(onStopped).toHaveBeenCalledWith('finish', { pendingSegmentCount: 1 });
    expect(track.stop).toHaveBeenCalledTimes(1);
  });

  it('discards buffered PCM when aborted', async () => {
    const { capture, onSegment, onStopped, pipeline } = setup();
    await pipeline.start();
    capture().emit(1, 2);

    pipeline.abort();
    capture().settle();

    expect(onSegment).not.toHaveBeenCalled();
    expect(onStopped).toHaveBeenCalledWith('abort', { pendingSegmentCount: 0 });
  });

  it('finishes before a hidden page can suspend audio processing', async () => {
    const visibilityDocument = new EventTarget() as EventTarget & {
      visibilityState: DocumentVisibilityState;
    };
    visibilityDocument.visibilityState = 'visible';
    vi.stubGlobal('document', visibilityDocument);
    const { capture, pipeline } = setup();
    await pipeline.start();

    visibilityDocument.visibilityState = 'hidden';
    visibilityDocument.dispatchEvent(new Event('visibilitychange'));

    expect(capture().stop).toHaveBeenCalledTimes(1);
  });

  it('does not become active when the page hides during capture startup', async () => {
    const visibilityDocument = new EventTarget() as EventTarget & {
      visibilityState: DocumentVisibilityState;
    };
    visibilityDocument.visibilityState = 'visible';
    vi.stubGlobal('document', visibilityDocument);
    const { capture, onStopped, pipeline, track } = setup();
    let resolveStart!: () => void;
    capture().start.mockImplementationOnce(() => new Promise<void>((resolve) => {
      resolveStart = resolve;
    }));

    const starting = pipeline.start();
    await vi.waitFor(() => expect(capture().start).toHaveBeenCalledOnce());
    visibilityDocument.visibilityState = 'hidden';
    visibilityDocument.dispatchEvent(new Event('visibilitychange'));
    expect(track.stop).toHaveBeenCalledTimes(1);
    resolveStart();

    await expect(starting).resolves.toBe(false);
    expect(capture().stop).toHaveBeenCalledTimes(1);
    capture().settle();
    expect(onStopped).toHaveBeenCalledWith('finish', { pendingSegmentCount: 0 });
  });

  it('retains captured audio as failed after the processor stops unexpectedly', async () => {
    const {
      capture,
      onError,
      onSegment,
      onStopRequested,
      onStopped,
      pipeline,
    } = setup();
    await pipeline.start();
    capture().emit(1, 2);
    const error = new Error('processor stopped');

    capture().fail(error);

    expect(onStopRequested).toHaveBeenCalledWith('error', {
      requestedAt: expect.any(Number),
    });
    expect(onError).toHaveBeenCalledWith(error);
    expect(onSegment).toHaveBeenCalledTimes(1);
    expect(onStopped).toHaveBeenCalledWith('error', { pendingSegmentCount: 1 });
  });

  it('upgrades a user stop to failure when the processor crashes while flushing', async () => {
    const {
      capture,
      onError,
      onStopRequested,
      onStopped,
      pipeline,
    } = setup();
    await pipeline.start();
    capture().emit(1, 2);
    pipeline.finish();
    const error = new Error('processor stopped while flushing');

    capture().fail(error);

    expect(onStopRequested).toHaveBeenCalledOnce();
    expect(onStopRequested).toHaveBeenCalledWith('finish', {
      requestedAt: expect.any(Number),
    });
    expect(onError).toHaveBeenCalledWith(error);
    expect(onStopped).toHaveBeenCalledWith('error', { pendingSegmentCount: 1 });
  });

  it('reports inactive when capture stops before asynchronous startup completes', async () => {
    const { capture, onStopped, pipeline } = setup();
    let resolveStart!: () => void;
    capture().start.mockImplementationOnce(() => new Promise<void>((resolve) => {
      resolveStart = resolve;
    }));

    const starting = pipeline.start();
    await vi.waitFor(() => expect(capture().start).toHaveBeenCalledOnce());
    capture().settle();
    resolveStart();

    await expect(starting).resolves.toBe(false);
    expect(onStopped).toHaveBeenCalledWith('finish', { pendingSegmentCount: 0 });
  });

  it('observes a microphone track ending while the worklet module loads', async () => {
    let resolveModule!: () => void;
    const addModule = vi.fn(() => new Promise<void>((resolve) => {
      resolveModule = resolve;
    }));
    class FakeAudioContext {
      state = 'running';
      readonly audioWorklet = { addModule };
      readonly destination = {};
      readonly close = vi.fn(async () => {
        this.state = 'closed';
      });
      readonly resume = vi.fn(async () => undefined);
      readonly createMediaStreamSource = vi.fn(() => ({
        connect: vi.fn(),
        disconnect: vi.fn(),
      }));
    }
    class FakeAudioWorkletNode {
      readonly port = {
        onmessage: null,
        postMessage: vi.fn(),
        close: vi.fn(),
      };
      onprocessorerror: (() => void) | null = null;
      readonly connect = vi.fn();
      readonly disconnect = vi.fn();
    }
    class FakeTrack extends EventTarget {
      readyState: MediaStreamTrackState = 'live';
      readonly stop = vi.fn();

      end(): void {
        this.readyState = 'ended';
        this.dispatchEvent(new Event('ended'));
      }
    }
    const track = new FakeTrack();
    const stream = {
      getTracks: () => [track],
    } as unknown as MediaStream;
    const onError = vi.fn();
    const onStopRequested = vi.fn();
    const onStopped = vi.fn();
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeAudioWorkletNode);
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:voice-worklet'),
      revokeObjectURL: vi.fn(),
    });
    const pipeline = new VoiceRecordingPipeline({
      stream,
      segmentMs: 1000,
      onSegment: vi.fn(),
      onError,
      onStopRequested,
      onStopped,
    });

    const starting = pipeline.start();
    await vi.waitFor(() => expect(addModule).toHaveBeenCalledOnce());
    track.end();
    resolveModule();

    await expect(starting).resolves.toBe(false);
    expect(onError).toHaveBeenCalledWith(expect.any(Error));
    expect(onStopRequested).toHaveBeenCalledWith('error', {
      requestedAt: expect.any(Number),
    });
    expect(onStopped).toHaveBeenCalledWith('error', { pendingSegmentCount: 0 });
  });

  it('reports no queued segment when stopping exactly at a segment boundary', async () => {
    const { capture, onStopped, pipeline } = setup();
    await pipeline.start();
    capture().emit(1, 2, 3, 4);

    pipeline.finish();
    capture().settle();

    expect(onStopped).toHaveBeenCalledWith('finish', { pendingSegmentCount: 0 });
  });

  it('counts every file segment delivered after the stop request', async () => {
    const { capture, onSegment, onStopped, pipeline } = setup();
    await pipeline.start();

    pipeline.finish();
    capture().emit(1, 2, 3, 4, 5, 6, 7, 8);
    capture().settle();

    expect(onSegment).toHaveBeenCalledTimes(2);
    expect(onStopped).toHaveBeenCalledWith('finish', { pendingSegmentCount: 2 });
  });
});

describe('voice PCM worklet', () => {
  it('delivers PCM before a full file segment has accumulated', () => {
    const oneMinute = 60 * 16_000;

    expect(voicePcmDeliverySamples(16_000, oneMinute)).toBe(4_000);
    expect(voicePcmDeliverySamples(16_000, 2_000)).toBe(2_000);
  });

  it('loads and emits bounded downsampled buffers on the rendering thread', () => {
    const postMessage = vi.fn();
    class FakeAudioWorkletProcessor {
      readonly port = {
        onmessage: null as ((event: { data: { type?: string } }) => void) | null,
        postMessage,
      };
    }
    let Processor: (new (options: {
      processorOptions: {
        targetSampleRate: number;
        chunkSamples: number;
      };
    }) => {
      port: FakeAudioWorkletProcessor['port'];
      process: (inputs: Float32Array[][]) => boolean;
    }) | null = null;
    const loadModule = new Function(
      'AudioWorkletProcessor',
      'registerProcessor',
      'sampleRate',
      voicePcmWorkletSource,
    );
    loadModule(
      FakeAudioWorkletProcessor,
      (_name: string, constructor: typeof Processor) => {
        Processor = constructor;
      },
      8,
    );

    const processor = new Processor!({
      processorOptions: {
        targetSampleRate: 4,
        chunkSamples: 4,
      },
    });
    expect(processor.process([[
      Float32Array.from([0, 0.25, 0.5, 0.75, 1, 0.75, 0.5, 0.25]),
    ]])).toBe(true);

    const sampleMessage = postMessage.mock.calls[0]?.[0] as {
      type: string;
      samples: ArrayBuffer;
    };
    expect(sampleMessage.type).toBe('samples');
    expect(new Int16Array(sampleMessage.samples)).toHaveLength(4);

    processor.port.onmessage?.({ data: { type: 'stop' } });
    expect(postMessage.mock.calls.at(-1)?.[0]).toEqual({ type: 'stopped' });
  });

  it('upsamples low-rate input before emitting target-rate PCM', () => {
    const postMessage = vi.fn();
    class FakeAudioWorkletProcessor {
      readonly port = {
        onmessage: null as ((event: { data: { type?: string } }) => void) | null,
        postMessage,
      };
    }
    let Processor: (new (options: {
      processorOptions: {
        targetSampleRate: number;
        chunkSamples: number;
      };
    }) => {
      port: FakeAudioWorkletProcessor['port'];
      process: (inputs: Float32Array[][]) => boolean;
    }) | null = null;
    const loadModule = new Function(
      'AudioWorkletProcessor',
      'registerProcessor',
      'sampleRate',
      voicePcmWorkletSource,
    );
    loadModule(
      FakeAudioWorkletProcessor,
      (_name: string, constructor: typeof Processor) => {
        Processor = constructor;
      },
      4,
    );

    const processor = new Processor!({
      processorOptions: {
        targetSampleRate: 8,
        chunkSamples: 8,
      },
    });
    expect(processor.process([[
      Float32Array.from([0, 1, 0, -1]),
    ]])).toBe(true);
    processor.port.onmessage?.({ data: { type: 'stop' } });

    const sampleMessage = postMessage.mock.calls[0]?.[0] as {
      type: string;
      samples: ArrayBuffer;
    };
    expect(sampleMessage.type).toBe('samples');
    expect(Array.from(new Int16Array(sampleMessage.samples))).toEqual([
      0,
      16384,
      32767,
      16384,
      0,
      -16384,
      -32768,
      -32768,
    ]);
    expect(postMessage.mock.calls.at(-1)?.[0]).toEqual({ type: 'stopped' });
  });
});

describe('deleteMapValueIfCurrent', () => {
  it('cannot let an old finalizer delete a replacement session', () => {
    const oldSession = {};
    const replacement = {};
    const sessions = new Map([['session', replacement]]);

    expect(deleteMapValueIfCurrent(sessions, 'session', oldSession)).toBe(false);
    expect(sessions.get('session')).toBe(replacement);
    expect(deleteMapValueIfCurrent(sessions, 'session', replacement)).toBe(true);
  });
});

describe('isVoiceControlDisabled', () => {
  it('requires explicit discard before a retained recording can be replaced', () => {
    expect(isVoiceControlDisabled(false, false, false, true)).toBe(true);
    expect(isVoiceControlDisabled(false, true, false, true)).toBe(false);
  });
});
