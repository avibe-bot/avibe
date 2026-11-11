import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  claimVoiceCapture,
  deleteMapValueIfCurrent,
  isVoiceControlDisabled,
  prewarmVoiceCapture,
  VOICE_AUDIO_START_TIMEOUT_MS,
  VOICE_RENDER_WINDOW_MS,
  VOICE_CAPTURE_STOP_TIMEOUT_MS,
  VOICE_MIN_SEGMENT_MS,
  voicePcmDeliverySamples,
  voicePcmWorkletSource,
  voiceWavMaxSamples,
  VoiceRecordingPipeline,
  type VoiceRecordingPipelineOptions,
} from './voiceRecording';
import { VOICE_RECORDER_SILENT_MS } from './voiceRecorderFallback';

// The reliability log: every take's capture trace goes through here.
const telemetry = vi.hoisted(() => ({ emit: vi.fn() }));
vi.mock('./voiceTelemetry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./voiceTelemetry')>()),
  emitVoiceTelemetry: telemetry.emit,
}));

const initialNavigatorLocks = Object.getOwnPropertyDescriptor(navigator, 'locks');

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

const setup = (options: {
  maxFileBytes?: number;
  overlapMs?: number;
  segmentMs?: number;
} = {}) => {
  const track = {
    stop: vi.fn(),
  };
  const stream = {
    getTracks: () => [track],
  } as unknown as MediaStream;
  const onSegment = vi.fn<(blob: Blob, metadata: { durationMs: number }) => void>();
  const onPcm = vi.fn<(samples: Int16Array) => void>();
  const onError = vi.fn();
  const onStopRequested = vi.fn();
  const onStopped = vi.fn();
  let capture: FakeCapture | null = null;
  const pipeline = new VoiceRecordingPipeline({
    stream,
    sampleRate: 4,
    segmentMs: options.segmentMs ?? 1000,
    maxFileBytes: options.maxFileBytes,
    overlapMs: options.overlapMs ?? 0,
    onSegment,
    onPcm,
    onError,
    onStopRequested,
    onStopped,
    createCapture: (_stream, sampleRate, segmentSamples, handlers) => {
      expect(sampleRate).toBe(4);
      expect(segmentSamples).toBe(
        Math.min(
          Math.round(4 * (options.segmentMs ?? 1000) / 1000),
          voiceWavMaxSamples(options.maxFileBytes) ?? Number.POSITIVE_INFINITY,
        ),
      );
      capture = new FakeCapture(handlers);
      return capture;
    },
  });
  return {
    capture: () => capture!,
    onError,
    onSegment,
    onPcm,
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
  Reflect.deleteProperty(navigator, 'locks');
  if (initialNavigatorLocks) Object.defineProperty(navigator, 'locks', initialNavigatorLocks);
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('VoiceRecordingPipeline', () => {
  it('delivers short PCM frames independently of WAV segmentation', async () => {
    const { capture, onPcm, onSegment, pipeline } = setup();
    await pipeline.start();
    capture().emit(1, 2, 3);

    expect(onPcm).toHaveBeenCalledTimes(1);
    expect(Array.from(onPcm.mock.calls[0]?.[0] ?? [])).toEqual([1, 2, 3]);
    expect(onSegment).not.toHaveBeenCalled();
  });

  it('passes the microphone level on only while it records', async () => {
    const onLevel = vi.fn();
    let handlers: CaptureHandlers & { onLevel?: (rms: number) => void } = null!;
    const stream = { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream;
    const pipeline = new VoiceRecordingPipeline({
      stream,
      sampleRate: 4,
      segmentMs: 1000,
      onSegment: vi.fn(),
      onStopped: vi.fn(),
      onLevel,
      createCapture: (_stream, _rate, _samples, next) => {
        handlers = next;
        return { start: async () => undefined, stop: vi.fn() };
      },
    });
    await pipeline.start();
    handlers.onLevel?.(0.2);
    pipeline.finish();
    // Flushing after the release: what was heard still lands, the meter goes quiet.
    handlers.onLevel?.(0.3);

    expect(onLevel.mock.calls).toEqual([[0.2]]);
  });

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

  it('keeps every WAV within the configured ASR byte limit', async () => {
    const { capture, onSegment, pipeline } = setup({
      maxFileBytes: 52,
      overlapMs: 250,
    });
    await pipeline.start();

    capture().emit(1, 2, 3, 4, 5, 6, 7, 8);
    pipeline.finish();
    capture().settle();

    expect(onSegment).toHaveBeenCalledTimes(3);
    expect(onSegment.mock.calls.map(([blob]) => blob.size)).toEqual([52, 52, 48]);
    expect(await Promise.all(onSegment.mock.calls.map(([blob]) => wavSamples(blob)))).toEqual([
      [1, 2, 3, 4],
      [4, 5, 6, 7],
      [7, 8],
    ]);
  });

  it('rejects a file limit below one practical segment', () => {
    const minimumSamples = Math.round(4 * VOICE_MIN_SEGMENT_MS / 1000);
    const belowMinimumBytes = 44 + ((minimumSamples - 1) * 2);
    expect(() => setup({
      maxFileBytes: belowMinimumBytes,
      segmentMs: VOICE_MIN_SEGMENT_MS + 1000,
    })).toThrow(
      'configured ASR file limit is too small for a practical WAV segment',
    );
  });

  it('overlaps adjacent segments without emitting an overlap-only tail', async () => {
    const { capture, onSegment, pipeline } = setup({ overlapMs: 500 });
    await pipeline.start();

    capture().emit(1, 2, 3, 4, 5, 6);
    pipeline.finish();
    capture().settle();

    expect(onSegment).toHaveBeenCalledTimes(2);
    expect(await Promise.all(onSegment.mock.calls.map(([blob]) => wavSamples(blob)))).toEqual([
      [1, 2, 3, 4],
      [3, 4, 5, 6],
    ]);
    expect(onSegment.mock.calls.map(([, metadata]) => metadata)).toEqual([
      { durationMs: 1000, final: false },
      { durationMs: 1000, overlapMs: 500, final: false },
    ]);
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
    expect(onSegment.mock.calls[0]?.[1]).toEqual({ durationMs: 500, final: true });
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

  it.each([
    ['the page is hidden', 'visibilitychange'],
    ['the page is left', 'pagehide'],
  ] as const)('releases the microphone at once when %s', async (_case, event) => {
    const visibilityDocument = new EventTarget() as EventTarget & {
      visibilityState: DocumentVisibilityState;
    };
    visibilityDocument.visibilityState = 'visible';
    const pageWindow = new EventTarget();
    vi.stubGlobal('document', visibilityDocument);
    vi.stubGlobal('window', pageWindow);
    const { capture, pipeline, track } = setup();
    await pipeline.start();

    if (event === 'visibilitychange') {
      visibilityDocument.visibilityState = 'hidden';
      visibilityDocument.dispatchEvent(new Event('visibilitychange'));
    } else {
      pageWindow.dispatchEvent(new Event('pagehide'));
    }

    // The tracks stop now, not after the capture's flush; the heard audio still flushes.
    expect(track.stop).toHaveBeenCalledTimes(1);
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

  it('reuses a prewarmed audio context and retries gesture-gated resume', async () => {
    const addModule = vi.fn(async () => undefined);
    class FakeAudioContext {
      static instance: FakeAudioContext | null = null;
      state = 'suspended';
      readonly audioWorklet = { addModule };
      readonly destination = {};
      readonly close = vi.fn(async () => {
        this.state = 'closed';
      });
      // The browser refuses a resume outside a gesture (the idle prewarm) and honours the press's.
      readonly resume = vi.fn(async () => {
        if (this.resume.mock.calls.length > 1) this.state = 'running';
      });
      readonly createMediaStreamSource = vi.fn(() => ({
        connect: vi.fn(),
        disconnect: vi.fn(),
      }));

      constructor() {
        FakeAudioContext.instance = this;
      }
    }
    class FakeAudioWorkletNode {
      static instance: FakeAudioWorkletNode | null = null;
      readonly port = {
        onmessage: null as ((event: {
          data: { type?: string; samples?: ArrayBuffer };
        }) => void) | null,
        postMessage: vi.fn(),
        close: vi.fn(),
      };
      onprocessorerror: (() => void) | null = null;
      readonly connect = vi.fn();
      readonly disconnect = vi.fn();

      constructor() {
        FakeAudioWorkletNode.instance = this;
      }
    }
    const track = new EventTarget() as EventTarget & {
      readyState: MediaStreamTrackState;
      stop: ReturnType<typeof vi.fn>;
    };
    track.readyState = 'live';
    track.stop = vi.fn();
    const stream = { getTracks: () => [track] } as unknown as MediaStream;
    const onStopped = vi.fn();
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeAudioWorkletNode);
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:voice-worklet'),
      revokeObjectURL: vi.fn(),
    });

    await prewarmVoiceCapture();
    expect(FakeAudioContext.instance?.resume).toHaveBeenCalledOnce();
    await prewarmVoiceCapture();
    expect(FakeAudioContext.instance?.resume).toHaveBeenCalledTimes(2);
    const pipeline = new VoiceRecordingPipeline({
      stream,
      segmentMs: 1000,
      onSegment: vi.fn(),
      onStopped,
    });
    await pipeline.start();

    expect(FakeAudioContext.instance).not.toBeNull();
    expect(addModule).toHaveBeenCalledOnce();
    expect(FakeAudioContext.instance?.createMediaStreamSource).toHaveBeenCalledOnce();

    pipeline.abort();
    FakeAudioWorkletNode.instance?.port.onmessage?.({ data: { type: 'stopped' } });
    expect(onStopped).toHaveBeenCalledWith('abort', { pendingSegmentCount: 0 });
    // The real worklet acknowledges stop through its message port. The test
    // drives that boundary so the shared context can be closed cleanly.
    await FakeAudioContext.instance?.close();
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

  it('retains delivered PCM when the worklet does not acknowledge stop', async () => {
    vi.useFakeTimers();
    class FakeAudioContext {
      state = 'running';
      readonly audioWorklet = { addModule: vi.fn(async () => undefined) };
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
      static instance: FakeAudioWorkletNode | null = null;
      readonly port = {
        onmessage: null as ((event: {
          data: { type?: string; samples?: ArrayBuffer };
        }) => void) | null,
        postMessage: vi.fn(),
        close: vi.fn(),
      };
      onprocessorerror: (() => void) | null = null;
      readonly connect = vi.fn();
      readonly disconnect = vi.fn();

      constructor() {
        FakeAudioWorkletNode.instance = this;
      }
    }
    const track = new EventTarget() as EventTarget & {
      readyState: MediaStreamTrackState;
      stop: ReturnType<typeof vi.fn>;
    };
    track.readyState = 'live';
    track.stop = vi.fn();
    const stream = {
      getTracks: () => [track],
    } as unknown as MediaStream;
    const onError = vi.fn();
    const onSegment = vi.fn();
    const onStopped = vi.fn();
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeAudioWorkletNode);
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:voice-worklet'),
      revokeObjectURL: vi.fn(),
    });
    const pipeline = new VoiceRecordingPipeline({
      stream,
      sampleRate: 4,
      segmentMs: 1000,
      overlapMs: 0,
      stopTimeoutMs: VOICE_CAPTURE_STOP_TIMEOUT_MS,
      onSegment,
      onError,
      onStopped,
    });
    await pipeline.start();
    const workletNode = FakeAudioWorkletNode.instance;
    expect(workletNode).not.toBeNull();
    workletNode.port.onmessage?.({
      data: { type: 'samples', samples: Int16Array.from([1, 2]).buffer },
    });

    pipeline.finish();
    await vi.advanceTimersByTimeAsync(VOICE_CAPTURE_STOP_TIMEOUT_MS);

    expect(onError).toHaveBeenCalledWith(expect.any(Error));
    expect(onSegment).toHaveBeenCalledOnce();
    expect(await wavSamples(onSegment.mock.calls[0]![0])).toEqual([1, 2]);
    expect(onStopped).toHaveBeenCalledWith('error', { pendingSegmentCount: 1 });
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

// WebKit's audio context as a take meets it on iOS: `interrupted` when the system takes the
// audio session (the microphone permission alert is one), and a resume that then waits for the
// interruption to end. These drive the real capture against that boundary.
describe('voice capture audio context', () => {
  type Resume = 'starts' | 'hangs';
  const contexts: FakeAudioContext[] = [];
  let nextResume: Resume[] = [];
  /** Rates whose contexts run and render nothing, as 48 kHz did on the owner's iPhone. */
  let stalledRates = new Set<number>();
  class FakeAudioContext {
    state: string = 'suspended';
    readonly sampleRate: number;
    /** This context runs and renders nothing (a stale one, or a stalled engine). */
    stalled: boolean;
    private runningSince: number | null = null;
    /** The context clock, in seconds: it moves while the context renders, and not while it stalls. */
    get currentTime(): number {
      if (this.state !== 'running' || this.stalled) return 0;
      this.runningSince ??= Date.now();
      return (Date.now() - this.runningSince) / 1000;
    }
    readonly resumeBehavior: Resume;
    readonly audioWorklet = { addModule: vi.fn(async () => undefined) };
    readonly destination = {};
    readonly close = vi.fn(async () => {
      this.state = 'closed';
    });
    readonly resume = vi.fn(() => {
      // An interrupted context resolves only once the interruption ends, which it need not.
      if (this.state === 'interrupted' || this.resumeBehavior === 'hangs') return new Promise<void>(() => undefined);
      this.state = 'running';
      return Promise.resolve();
    });
    readonly createMediaStreamSource = vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() }));

    constructor(options?: { sampleRate?: number }) {
      this.sampleRate = options?.sampleRate ?? 48_000;
      this.stalled = stalledRates.has(this.sampleRate);
      this.resumeBehavior = nextResume.shift() ?? 'starts';
      contexts.push(this);
    }
  }
  const nodes: FakeAudioWorkletNode[] = [];
  class FakeAudioWorkletNode {
    readonly port = {
      onmessage: null as ((event: { data: { type?: string; samples?: ArrayBuffer } }) => void) | null,
      postMessage: vi.fn(),
      close: vi.fn(),
    };
    onprocessorerror = null;
    readonly connect = vi.fn();
    readonly disconnect = vi.fn();
    constructor(readonly context: FakeAudioContext, _name?: string, readonly options?: unknown) {
      nodes.push(this);
    }
    /** The rendering thread delivering a chunk of frames. */
    deliver(...samples: number[]): void {
      this.port.onmessage?.({ data: { type: 'samples', samples: Int16Array.from(samples).buffer } });
    }
  }
  /** A MediaRecorder on the microphone, as an iPhone's records it (audio/mp4). */
  const recorders: FakeMediaRecorder[] = [];
  class FakeMediaRecorder {
    static isTypeSupported = (type: string) => type === 'audio/mp4';
    state: 'inactive' | 'recording' = 'inactive';
    readonly mimeType: string;
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    constructor(readonly stream: MediaStream, options?: { mimeType?: string }) {
      this.mimeType = options?.mimeType ?? 'audio/mp4';
      recorders.push(this);
    }
    start(): void {
      this.state = 'recording';
    }
    stop(): void {
      if (this.state === 'inactive') return;
      this.state = 'inactive';
      queueMicrotask(() => this.onstop?.());
    }
    /** The microphone giving the recorder a chunk of its recording. */
    data(bytes = 100): void {
      this.ondataavailable?.({ data: new Blob([new Uint8Array(bytes)], { type: this.mimeType }) });
    }
  }
  /** What decoding a recording gives: speech, silence, or a format this browser cannot read. */
  let decodes: 'speech' | 'silence' | 'fails' = 'speech';
  class FakeOfflineAudioContext {
    constructor(readonly channels: number, readonly length: number, readonly sampleRate: number) {}
    async decodeAudioData(): Promise<{ numberOfChannels: number; length: number; getChannelData: () => Float32Array }> {
      if (decodes === 'fails') throw new DOMException('Unable to decode audio data', 'EncodingError');
      // Half a second at the context's rate, resampled as the spec requires.
      const length = this.sampleRate / 2;
      return { numberOfChannels: 1, length, getChannelData: () => new Float32Array(length).fill(decodes === 'speech' ? 0.25 : 0) };
    }
  }

  const microphone = () => {
    const track = Object.assign(new EventTarget(), { readyState: 'live', muted: false, enabled: true, stop: vi.fn() });
    return { track, stream: { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream };
  };
  const pipelines: { abort: () => void }[] = [];
  const pipelineOn = (stream: MediaStream, options: Partial<VoiceRecordingPipelineOptions> = {}) => {
    const pipeline = new VoiceRecordingPipeline({
      stream,
      segmentMs: 1000,
      onSegment: vi.fn(),
      onStopped: vi.fn(),
      ...options,
    });
    pipelines.push(pipeline);
    return pipeline;
  };

  beforeEach(() => {
    contexts.length = 0;
    nodes.length = 0;
    pipelines.length = 0;
    nextResume = [];
    stalledRates = new Set();
    telemetry.emit.mockReset();
    recorders.length = 0;
    decodes = 'speech';
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
    vi.stubGlobal('OfflineAudioContext', FakeOfflineAudioContext);
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeAudioWorkletNode);
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:voice-worklet');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
  });
  afterEach(async () => {
    pipelines.forEach((pipeline) => pipeline.abort());
    // The prewarmed context is page-wide: close it so the next test builds its own.
    await Promise.all(contexts.map((context) => context.close()));
  });

  it('records the take after a permission alert interrupted the prewarmed context', async () => {
    await prewarmVoiceCapture(); // the first press, inside its gesture
    const first = contexts[0];
    expect(first.state).toBe('running');
    first.state = 'interrupted'; // the system alert that asks for the microphone

    await prewarmVoiceCapture(); // the next press
    const { stream } = microphone();
    await expect(pipelineOn(stream).start()).resolves.toBe(true);

    expect(first.close).toHaveBeenCalled();
    expect(first.createMediaStreamSource).not.toHaveBeenCalled();
    expect(contexts).toHaveLength(2);
    expect(contexts[1].state).toBe('running');
    expect(contexts[1].createMediaStreamSource).toHaveBeenCalledWith(stream);
  });

  it('rebuilds on a fresh context when the prewarmed one does not start rendering', async () => {
    vi.useFakeTimers();
    nextResume = ['hangs'];
    await prewarmVoiceCapture();
    const { stream } = microphone();
    const starting = pipelineOn(stream).start();
    await vi.advanceTimersByTimeAsync(VOICE_AUDIO_START_TIMEOUT_MS);

    await expect(starting).resolves.toBe(true);
    expect(contexts[0].close).toHaveBeenCalled();
    expect(contexts).toHaveLength(2);
    expect(contexts[1].state).toBe('running');
    expect(contexts[1].createMediaStreamSource).toHaveBeenCalledWith(stream);
  });

  it('records a take whose audio never starts without Web Audio, instead of failing it', async () => {
    vi.useFakeTimers();
    nextResume = ['hangs', 'hangs'];
    await prewarmVoiceCapture();
    const { track, stream } = microphone();
    const onCaptureState = vi.fn();
    const starting = pipelineOn(stream, { onCaptureState }).start();
    await vi.advanceTimersByTimeAsync(2 * VOICE_AUDIO_START_TIMEOUT_MS);

    await expect(starting).resolves.toBe(true);
    expect(recorders).toHaveLength(1);
    expect(onCaptureState).toHaveBeenCalledWith('recorder');
    expect(track.stop).not.toHaveBeenCalled();
    expect(contexts.every((context) => context.state === 'closed')).toBe(true);
  });

  it('fails a take whose audio never starts where nothing else can record it', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('MediaRecorder', undefined);
    nextResume = ['hangs', 'hangs'];
    await prewarmVoiceCapture();
    const { track, stream } = microphone();
    const failed = expect(pipelineOn(stream).start()).rejects.toThrow('voice audio context did not start');
    await vi.advanceTimersByTimeAsync(2 * VOICE_AUDIO_START_TIMEOUT_MS);

    await failed;
    expect(track.stop).toHaveBeenCalled();
    expect(contexts.every((context) => context.state === 'closed')).toBe(true);
  });

  it('rebuilds a prewarmed context that lived through a hidden page, though it reports running', async () => {
    // On iOS the app's audio stops in the background; the context comes back `running` and
    // renders nothing (owner's iPhone, 2026-11-04: zero frames on every take after a trip away).
    const page = Object.assign(new EventTarget(), { visibilityState: 'visible' as DocumentVisibilityState });
    vi.stubGlobal('document', page);
    vi.stubGlobal('window', new EventTarget());
    vi.resetModules();
    const voice = await import('./voiceRecording');
    await voice.prewarmVoiceCapture();
    const before = contexts.at(-1)!;
    expect(before.state).toBe('running');
    page.visibilityState = 'hidden';
    page.dispatchEvent(new Event('visibilitychange'));
    page.visibilityState = 'visible';
    page.dispatchEvent(new Event('visibilitychange'));

    await voice.prewarmVoiceCapture(); // the next press
    const { stream } = microphone();
    const pipeline = new voice.VoiceRecordingPipeline({ stream, segmentMs: 1000, onSegment: vi.fn(), onStopped: vi.fn() });
    pipelines.push(pipeline);
    await expect(pipeline.start()).resolves.toBe(true);

    expect(before.close).toHaveBeenCalled();
    expect(before.createMediaStreamSource).not.toHaveBeenCalled();
    expect(contexts.at(-1)).not.toBe(before);
    expect(contexts.at(-1)!.createMediaStreamSource).toHaveBeenCalledWith(stream);
  });

  it('rebuilds a take whose graph delivers no frames on its microphone, within the take', async () => {
    vi.useFakeTimers();
    await prewarmVoiceCapture();
    const { stream, track } = microphone();
    const onPcm = vi.fn();
    await pipelineOn(stream, { onPcm }).start();
    const [silent] = nodes;

    await vi.advanceTimersByTimeAsync(VOICE_RENDER_WINDOW_MS);
    expect(silent.context.close).toHaveBeenCalled();
    expect(nodes).toHaveLength(2);
    expect(nodes[1].context.createMediaStreamSource).toHaveBeenCalledWith(stream);
    expect(track.stop).not.toHaveBeenCalled();

    // Frames, even exact zeros (a quiet room behind noise suppression), are a rendering graph.
    nodes[1].deliver(0, 0, 0);
    await vi.advanceTimersByTimeAsync(3 * VOICE_RENDER_WINDOW_MS);
    expect(nodes).toHaveLength(2);
    expect(onPcm).toHaveBeenCalledOnce();
  });

  it('then records it without Web Audio, on the same microphone, when the rebuild delivers nothing too', async () => {
    vi.useFakeTimers();
    const getUserMedia = vi.fn(async () => microphone().stream);
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } });
    const { stream, track } = microphone();
    const onCaptureState = vi.fn();
    const onStopped = vi.fn();
    await pipelineOn(stream, { onCaptureState, onStopped }).start();

    await vi.advanceTimersByTimeAsync(VOICE_RENDER_WINDOW_MS); // rebuilt on the same microphone
    await vi.advanceTimersByTimeAsync(VOICE_RENDER_WINDOW_MS); // then recorded without Web Audio

    expect(recorders).toHaveLength(1);
    expect(recorders[0].stream).toBe(stream);
    expect(recorders[0].state).toBe('recording');
    expect(onCaptureState).toHaveBeenCalledWith('recorder');
    // Never a second microphone, and never a take ended for want of frames.
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(track.stop).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(10 * VOICE_RENDER_WINDOW_MS);
    expect(onStopped).not.toHaveBeenCalled();
  });

  it('finishes a take released while it is being rebuilt', async () => {
    vi.useFakeTimers();
    const { stream } = microphone();
    const onStopped = vi.fn();
    const pipeline = pipelineOn(stream, { onStopped });
    await pipeline.start();

    vi.advanceTimersByTime(VOICE_RENDER_WINDOW_MS); // the rebuild starts...
    pipeline.finish(); // ...and the hold is released before it has a graph
    await vi.advanceTimersByTimeAsync(0);

    expect(recorders).toHaveLength(0);
    expect(onStopped).toHaveBeenCalledWith('finish', expect.anything());
  });

  it('leaves a muted microphone to the system', async () => {
    vi.useFakeTimers();
    const { stream, track } = microphone();
    track.muted = true;
    await pipelineOn(stream).start();
    await vi.advanceTimersByTimeAsync(3 * VOICE_RENDER_WINDOW_MS);
    expect(nodes).toHaveLength(1);
  });

  /** The owner's iPhone. */
  const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0.1 Mobile/15E148 Safari/604.1';

  /** The capture traces the takes logged. */
  const traces = () => telemetry.emit.mock.calls
    .map(([event]) => event as Record<string, unknown>)
    .filter((event) => event.event === 'capture_trace');

  describe('when real-time audio stalls', () => {
    /** A fresh module: what the page found about its engine is the page's (lib/voiceEngine). */
    const freshVoice = async () => {
      vi.resetModules();
      return import('./voiceRecording');
    };
    beforeEach(() => {
      vi.stubGlobal('navigator', { userAgent: IPHONE, mediaDevices: { getUserMedia: vi.fn() } });
    });
    const takeOn = (voice: typeof import('./voiceRecording'), options: Partial<VoiceRecordingPipelineOptions> = {}) => {
      const pipeline = new voice.VoiceRecordingPipeline({ stream: microphone().stream, segmentMs: 1000, onSegment: vi.fn(), onStopped: vi.fn(), ...options });
      pipelines.push(pipeline);
      return pipeline;
    };

    it('goes on in a fresh context, live words kept, when only the prewarmed one stood still', async () => {
      vi.useFakeTimers();
      const voice = await freshVoice();
      await voice.prewarmVoiceCapture();
      contexts[0].stalled = true; // stale, not the engine
      const onCaptureState = vi.fn();
      const onPcm = vi.fn();
      await takeOn(voice, { onCaptureState, onPcm }).start();
      await vi.advanceTimersByTimeAsync(1_000);

      const live = nodes.at(-1)!;
      expect(live.context).not.toBe(contexts[0]);
      expect(live.context.sampleRate).toBe(48_000);
      live.deliver(1, 2, 3);
      expect(onPcm).toHaveBeenCalledOnce();
      expect(onCaptureState).not.toHaveBeenCalled();
      expect(contexts[0].close).toHaveBeenCalled();
      live.port.onmessage?.({ data: { type: 'stopped' } });
    });

    it('goes on at 44.1 kHz where 16 kHz stalls too', async () => {
      vi.useFakeTimers();
      stalledRates = new Set([48_000, 16_000]);
      const voice = await freshVoice();
      const onCaptureState = vi.fn();
      await takeOn(voice, { onCaptureState }).start();
      await vi.advanceTimersByTimeAsync(1_000);
      expect(nodes.at(-1)!.context.sampleRate).toBe(44_100);
      expect(onCaptureState).not.toHaveBeenCalled();
    });

    it('tries no other rate where the browser cannot connect a microphone at one (Firefox)', async () => {
      vi.useFakeTimers();
      vi.stubGlobal('navigator', { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 15.6; rv:146.0) Gecko/20100101 Firefox/146.0', mediaDevices: { getUserMedia: vi.fn() } });
      stalledRates = new Set([48_000]);
      const voice = await freshVoice();
      const onCaptureState = vi.fn();
      await takeOn(voice, { onCaptureState }).start();
      await vi.advanceTimersByTimeAsync(1_000);
      expect(contexts.every((context) => context.sampleRate === 48_000)).toBe(true);
      expect(onCaptureState).toHaveBeenCalledWith('recorder');
    });

    it('records the take without Web Audio when no rate renders, and transcribes what it recorded', async () => {
      vi.useFakeTimers();
      stalledRates = new Set([48_000, 16_000, 44_100]);
      const voice = await freshVoice();
      await voice.prewarmVoiceCapture();
      const onCaptureState = vi.fn();
      const onPcm = vi.fn();
      const onSegment = vi.fn();
      const onStopped = vi.fn();
      const pipeline = takeOn(voice, { onCaptureState, onPcm, onSegment, onStopped });
      await pipeline.start();
      await vi.advanceTimersByTimeAsync(1_000);

      expect(onCaptureState).toHaveBeenCalledWith('recorder');
      const [recorder] = recorders;
      expect(recorder.state).toBe('recording');
      recorder.data(4_000);
      pipeline.finish();
      await vi.advanceTimersByTimeAsync(0);

      // Its recording, decoded to the pipeline's 16 kHz PCM, is segmented like any take's.
      expect(onPcm).toHaveBeenCalled();
      expect(onPcm.mock.calls.reduce((total, [samples]) => total + (samples as Int16Array).length, 0)).toBe(8_000);
      expect(onSegment).toHaveBeenLastCalledWith(expect.any(Blob), expect.objectContaining({ final: true }));
      expect(onStopped).toHaveBeenCalledWith('finish', expect.anything());
      expect(traces().at(-1)).toMatchObject({ fallbackLevel: 'L4', stalledRates: [16_000, 44_100, 48_000], captureClass: 'sound', decodedSegments: 1, fileSegments: 0, recorderChunks: 1, recorderBytes: 4_000 });

      // The page remembers: the next take is recorded without Web Audio from its first moment.
      const nodesBefore = nodes.length;
      const next = takeOn(voice, { onCaptureState });
      await next.start();
      expect(recorders).toHaveLength(2);
      expect(nodes).toHaveLength(nodesBefore);
      next.abort();
      await vi.advanceTimersByTimeAsync(1_000);
      expect(traces().at(-1)).toMatchObject({ pageStalled: true, outcome: 'cancelled' });
    });

    it('transcribes a recording this browser cannot decode as it is, unjudged', async () => {
      vi.useFakeTimers();
      stalledRates = new Set([48_000, 16_000, 44_100]);
      decodes = 'fails';
      const voice = await freshVoice();
      const onPcm = vi.fn();
      const onSegment = vi.fn();
      const pipeline = takeOn(voice, { onPcm, onSegment });
      await pipeline.start();
      await vi.advanceTimersByTimeAsync(1_000);
      recorders[0].data(4_000);
      pipeline.finish();
      await vi.advanceTimersByTimeAsync(0);

      expect(onPcm).not.toHaveBeenCalled();
      // Its sound was never judged: the take's silence gate must not speak for it.
      expect(onSegment).toHaveBeenCalledExactlyOnceWith(
        expect.objectContaining({ type: 'audio/mp4', size: 4_000 }),
        expect.objectContaining({ final: true, unjudged: true }),
      );
      expect(traces().at(-1)).toMatchObject({ fallbackLevel: 'L4', captureClass: 'recorded', decodedSegments: 0, fileSegments: 1 });
    });

    it('says so while the microphone gives the recording nothing, and again once it does', async () => {
      vi.useFakeTimers();
      stalledRates = new Set([48_000, 16_000, 44_100]);
      const voice = await freshVoice();
      const onCaptureState = vi.fn();
      await takeOn(voice, { onCaptureState, segmentMs: 60_000 }).start();
      await vi.advanceTimersByTimeAsync(1_000);
      expect(onCaptureState.mock.calls).toEqual([['recorder']]);

      await vi.advanceTimersByTimeAsync(VOICE_RECORDER_SILENT_MS);
      expect(onCaptureState).toHaveBeenLastCalledWith('no_sound');
      recorders[0].data();
      expect(onCaptureState).toHaveBeenLastCalledWith('recorder');
    });

    it('keeps what was recorded of a take released while a rendering context is looked for', async () => {
      vi.useFakeTimers();
      stalledRates = new Set([48_000, 16_000, 44_100]);
      const voice = await freshVoice();
      const onPcm = vi.fn();
      const onStopped = vi.fn();
      const pipeline = takeOn(voice, { onPcm, onStopped });
      await pipeline.start();
      await vi.advanceTimersByTimeAsync(450); // stalled; the search is under way
      recorders[0].data(2_000);
      pipeline.finish();
      await vi.advanceTimersByTimeAsync(1_000);

      expect(onPcm).toHaveBeenCalled();
      expect(onStopped).toHaveBeenCalledWith('finish', expect.anything());
      expect(nodes.every((node) => node.context.state === 'closed')).toBe(true);
    });
  });

  describe('its capture trace', () => {
    /** A microphone with what a real one carries that the log must never hold. */
    const named = () => {
      const mic = microphone();
      Object.assign(mic.track, {
        id: 'track-id-secret',
        label: 'iPhone Microphone',
        getSettings: () => ({ deviceId: 'device-id-secret', groupId: 'group-id-secret' }),
      });
      return mic;
    };

    beforeEach(() => {
      telemetry.emit.mockReset();
    });

    it('says, stage by stage, how a Home Screen app take whose engine stalled went on at another rate, and nothing private', async () => {
      vi.useFakeTimers();
      vi.stubGlobal('window', Object.assign(new EventTarget(), { matchMedia: (query: string) => ({ matches: query === '(display-mode: standalone)' }) }));
      vi.stubGlobal('navigator', { userAgent: IPHONE, mediaDevices: { getUserMedia: vi.fn() }, audioSession: { type: 'auto', state: 'active' } });
      // The owner's iPhone, 2026-11-07: every 48 kHz context runs with its clock standing still.
      stalledRates = new Set([48_000]);
      vi.resetModules();
      const voice = await import('./voiceRecording');
      const { VoiceCaptureTrace: Trace } = await import('./voiceCaptureTrace');
      await voice.prewarmVoiceCapture();
      const { stream } = named();
      const trace = new Trace('hold', 'dictation-1');
      trace.micSettled(trace.mark());
      const onCaptureState = vi.fn();
      const onPcm = vi.fn();
      const pipeline = new voice.VoiceRecordingPipeline({ stream, trace, segmentMs: 1000, onSegment: vi.fn(), onStopped: vi.fn(), onCaptureState, onPcm });
      pipelines.push(pipeline);
      await pipeline.start();

      // Its clock stands still: fresh contexts at the default rate, 16 kHz and 44.1 kHz are tried at
      // once, and 16 kHz, the capture's own rate, carries the take.
      await vi.advanceTimersByTimeAsync(1_000);
      const live = nodes.at(-1)!;
      expect(live.context.sampleRate).toBe(16_000);
      live.deliver(0, 0, 0);
      expect(onPcm).toHaveBeenCalledOnce();
      // The surface never left live words: the quiet recording meanwhile is dropped.
      expect(onCaptureState).not.toHaveBeenCalled();
      expect(recorders[0].state).toBe('inactive');
      pipeline.finish();
      live.port.onmessage?.({ data: { type: 'stopped' } });

      const event = traces()[0];
      expect(event).toMatchObject({
        event: 'capture_trace',
        outcome: 'success',
        dictationId: 'dictation-1',
        entry: 'hold',
        surface: 'standalone',
        captureClass: 'silence',
        fallbackLevel: 'L0',
        stalledRates: [48_000],
        contextSampleRate: 16_000,
        stop: 'finish',
        mic: 'ok',
        prewarm: 'reused',
        audioSession: 'auto',
        audioSessionState: 'active',
        recorderTypes: ['audio/mp4'],
        fallback: 'recorder',
        recorderType: 'audio/mp4',
        trackEvents: [],
      });
      expect((event.attempts as Record<string, unknown>[]).map(({ kind, end, sampleRate, engine, clockAdvanceMs }) => ({ kind, end, sampleRate, engine, clockAdvanceMs }))).toEqual([
        { kind: 'initial', end: 'stalled', sampleRate: 48_000, engine: 'stalled', clockAdvanceMs: 0 },
        { kind: 'recorder', end: 'stopped', sampleRate: undefined, engine: undefined, clockAdvanceMs: undefined },
        { kind: 'context', end: 'rendered', sampleRate: 16_000, engine: 'healthy', clockAdvanceMs: expect.any(Number) },
      ]);
      // The shape the server keeps (vibe/ui_server.py asr_telemetry), and nothing a device names.
      expect(Object.keys(event).sort()).toEqual([
        'attempts', 'audioSession', 'audioSessionState', 'audioSessionStateEnd', 'captureClass', 'chunks', 'contextSampleRate', 'decodedSegments',
        'dictationId', 'entry', 'event', 'fallback', 'fallbackLevel', 'fileSegments', 'hiddenCount', 'levels', 'mic', 'micMs', 'origin', 'outcome', 'pageAgeMs',
        'peakDbfs', 'prewarm', 'prewarmAgeMs', 'recorderBytes', 'recorderChunks', 'recorderType', 'recorderTypes', 'stalledRates', 'stop', 'surface',
        'totalMs', 'trackEvents',
      ]);
      const attemptKeys = new Set(['kind', 'atMs', 'contextState', 'resumedState', 'resumeMs', 'sampleRate', 'module', 'moduleMs', 'chunks', 'firstFrameMs', 'levels', 'clockAdvanceMs', 'renderCalls', 'emptyInputs', 'engine', 'trackStart', 'trackEnd', 'end']);
      for (const attempt of event.attempts as Record<string, unknown>[]) {
        expect(Object.keys(attempt).filter((key) => !attemptKeys.has(key))).toEqual([]);
      }
      const logged = JSON.stringify(event);
      for (const secret of ['iPhone', 'track-id-secret', 'device-id-secret', 'group-id-secret']) expect(logged).not.toContain(secret);

      // The page remembers: the next press prewarms at 16 kHz, and its take starts there.
      await voice.prewarmVoiceCapture();
      expect(contexts.at(-1)!.sampleRate).toBe(16_000);
      const next = new voice.VoiceRecordingPipeline({ stream: microphone().stream, segmentMs: 1000, onSegment: vi.fn(), onStopped: vi.fn() });
      pipelines.push(next);
      await next.start();
      expect(nodes.at(-1)!.context.sampleRate).toBe(16_000);
      expect(traces()).toHaveLength(1);
    });

    it('tells a graph that renders with nothing coming in, by its heartbeat', async () => {
      vi.useFakeTimers();
      const { stream } = microphone();
      const pipeline = pipelineOn(stream);
      await pipeline.start();
      // The clock moves and the worklet is called, every time with no input at all.
      nodes[0].port.onmessage?.({ data: { type: 'tick', calls: 282, empty: 282 } } as never);
      await vi.advanceTimersByTimeAsync(VOICE_RENDER_WINDOW_MS);
      pipeline.finish();
      nodes[1].port.onmessage?.({ data: { type: 'stopped' } });

      const [empty] = traces()[0].attempts as Record<string, unknown>[];
      expect(empty).toMatchObject({ kind: 'initial', end: 'no_frames', engine: 'healthy', renderCalls: 282, emptyInputs: 282, clockAdvanceMs: VOICE_RENDER_WINDOW_MS });
      // The capture asks every graph's worklet for that heartbeat.
      expect(nodes.map((node) => (node.options as { processorOptions: { tickMs?: number } }).processorOptions.tickMs)).toEqual([250, 250]);
    });

    it('logs a released take that delivered frames as a success', async () => {
      vi.useFakeTimers();
      const { stream } = microphone();
      const pipeline = pipelineOn(stream);
      await pipeline.start();
      nodes[0].deliver(0, 0, 0);
      pipeline.finish();
      nodes[0].port.onmessage?.({ data: { type: 'stopped' } });
      pipeline.abort();

      expect(traces()).toEqual([expect.objectContaining({ outcome: 'success', stop: 'finish', chunks: 1 })]);
    });

    it.each([
      ['a microphone the system holds back', true, [0, 0, 0], 'muted'],
      ['a graph that delivers only silence', false, [0, 0, 0], 'silence'],
      ['a microphone that hears speech', false, Array.from({ length: 2048 }, () => 8000), 'sound'],
    ] as const)('tells %s (%s)', async (_name, muted, samples, delivered) => {
      vi.useFakeTimers();
      const { stream, track } = microphone();
      track.muted = muted;
      const pipeline = pipelineOn(stream);
      await pipeline.start();
      nodes[0].deliver(...samples);
      pipeline.finish();
      nodes[0].port.onmessage?.({ data: { type: 'stopped' } });

      expect(traces()).toEqual([expect.objectContaining({ captureClass: delivered })]);
    });

    it('records the microphone muting and coming back while it records', async () => {
      vi.useFakeTimers();
      const { stream, track } = microphone();
      const pipeline = pipelineOn(stream);
      await pipeline.start();
      track.muted = true;
      track.dispatchEvent(new Event('mute'));
      track.muted = false;
      track.dispatchEvent(new Event('unmute'));
      pipeline.finish();
      nodes[0].port.onmessage?.({ data: { type: 'stopped' } });

      expect(traces()[0]).toMatchObject({ captureClass: 'muted', trackEvents: [['mute', expect.any(Number)], ['unmute', expect.any(Number)]] });
    });

    it('names why a prewarmed context was not used: it lived through a hidden page', async () => {
      const page = Object.assign(new EventTarget(), { visibilityState: 'visible' as DocumentVisibilityState });
      vi.stubGlobal('document', page);
      vi.stubGlobal('window', new EventTarget());
      vi.resetModules();
      const voice = await import('./voiceRecording');
      await voice.prewarmVoiceCapture();
      page.visibilityState = 'hidden';
      page.dispatchEvent(new Event('visibilitychange'));
      page.visibilityState = 'visible';
      page.dispatchEvent(new Event('visibilitychange'));

      const { stream } = microphone();
      const pipeline = new voice.VoiceRecordingPipeline({ stream, segmentMs: 1000, onSegment: vi.fn(), onStopped: vi.fn() });
      pipelines.push(pipeline);
      await pipeline.start();
      pipeline.abort();
      nodes.at(-1)!.port.onmessage?.({ data: { type: 'stopped' } });

      expect(traces()).toEqual([expect.objectContaining({ outcome: 'cancelled', prewarm: 'none', prewarmDrop: 'hidden', hiddenCount: 1, sinceHiddenMs: expect.any(Number) })]);
    });
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

  it('reports the input level every levelMs, apart from the PCM, when asked', () => {
    const postMessage = vi.fn();
    class FakeAudioWorkletProcessor {
      readonly port = {
        onmessage: null as ((event: { data: { type?: string } }) => void) | null,
        postMessage,
      };
    }
    let Processor: (new (options: {
      processorOptions: { targetSampleRate: number; chunkSamples: number; levelMs?: number };
    }) => { process: (inputs: Float32Array[][]) => boolean }) | null = null;
    new Function('AudioWorkletProcessor', 'registerProcessor', 'sampleRate', voicePcmWorkletSource)(
      FakeAudioWorkletProcessor,
      (_name: string, constructor: typeof Processor) => {
        Processor = constructor;
      },
      1000,
    );

    // 1 kHz input, a level every 4 ms: four samples each.
    const processor = new Processor!({ processorOptions: { targetSampleRate: 1000, chunkSamples: 1000, levelMs: 4 } });
    processor.process([[Float32Array.from([0.5, -0.5, 0.5, -0.5, 0, 0, 0, 0, 0.1])]]);
    const levels = postMessage.mock.calls.map(([message]) => message).filter((message) => message.type === 'level');
    expect(levels).toEqual([{ type: 'level', rms: 0.5 }, { type: 'level', rms: 0 }]);

    // Off unless asked for: the PCM alone.
    postMessage.mockClear();
    new Processor!({ processorOptions: { targetSampleRate: 1000, chunkSamples: 2 } })
      .process([[Float32Array.from([0.5, -0.5, 0.5, -0.5])]]);
    expect(postMessage.mock.calls.every(([message]) => message.type === 'samples')).toBe(true);
  });

  it('reports a heartbeat of its calls, and of calls with no input, every tickMs when asked', () => {
    const postMessage = vi.fn();
    class FakeAudioWorkletProcessor {
      readonly port = { onmessage: null, postMessage };
    }
    let Processor: (new (options: {
      processorOptions: { targetSampleRate: number; chunkSamples: number; tickMs?: number };
    }) => { process: (inputs: Float32Array[][]) => boolean }) | null = null;
    new Function('AudioWorkletProcessor', 'registerProcessor', 'sampleRate', voicePcmWorkletSource)(
      FakeAudioWorkletProcessor,
      (_name: string, constructor: typeof Processor) => {
        Processor = constructor;
      },
      12_800,
    );

    // 12.8 kHz in render quanta of 128 frames: a tick every 10 calls for 100 ms.
    const processor = new Processor!({ processorOptions: { targetSampleRate: 16_000, chunkSamples: 4_000, tickMs: 100 } });
    for (let call = 0; call < 10; call += 1) processor.process([[]]);
    const ticks = () => postMessage.mock.calls.map(([message]) => message).filter((message) => message.type === 'tick');
    expect(ticks()).toEqual([{ type: 'tick', calls: 1, empty: 1 }, { type: 'tick', calls: 10, empty: 10 }]);

    // Off unless asked for.
    postMessage.mockClear();
    new Processor!({ processorOptions: { targetSampleRate: 16_000, chunkSamples: 4_000 } }).process([[]]);
    expect(ticks()).toEqual([]);
  });

  // A context at 16 kHz or 44.1 kHz carries a take whose 48 kHz engine stalled (lib/voiceEngine).
  const processorAt = (sampleRate: number, chunkSamples: number) => {
    const postMessage = vi.fn();
    class FakeAudioWorkletProcessor {
      readonly port = { onmessage: null as ((event: { data: { type?: string } }) => void) | null, postMessage };
    }
    let Processor: (new (options: { processorOptions: { targetSampleRate: number; chunkSamples: number } }) => {
      port: { onmessage: ((event: { data: { type?: string } }) => void) | null };
      process: (inputs: Float32Array[][]) => boolean;
    }) | null = null;
    new Function('AudioWorkletProcessor', 'registerProcessor', 'sampleRate', voicePcmWorkletSource)(
      FakeAudioWorkletProcessor,
      (_name: string, constructor: typeof Processor) => {
        Processor = constructor;
      },
      sampleRate,
    );
    const processor = new Processor!({ processorOptions: { targetSampleRate: 16_000, chunkSamples } });
    const emitted = () => postMessage.mock.calls
      .map(([message]) => message as { type: string; samples?: ArrayBuffer })
      .filter((message) => message.type === 'samples')
      .flatMap((message) => Array.from(new Int16Array(message.samples!)));
    return { processor, emitted };
  };

  it('passes a 16 kHz context\'s frames through unchanged', () => {
    const { processor, emitted } = processorAt(16_000, 4);
    const input = Float32Array.from([0, 0.25, -0.25, 0.5, -0.5, 1, -1, 0.125]);
    processor.process([[input]]);

    expect(emitted()).toEqual(Array.from(input, (sample) => (sample < 0 ? Math.round(sample * 32_768) : Math.round(sample * 32_767))));
  });

  it('downsamples a 44.1 kHz context to 16 kHz, a ratio that is not a whole number', () => {
    const { processor, emitted } = processorAt(44_100, 16_000);
    // One second of a steady level, in render quanta of 128 frames.
    const quantum = new Float32Array(128).fill(0.5);
    for (let frames = 0; frames < 44_100; frames += 128) processor.process([[quantum]]);
    processor.port.onmessage?.({ data: { type: 'stop' } });

    const samples = emitted();
    // 44,160 frames in (345 quanta) at 16/44.1 is 16,021.8: 16,021 averages, and the last partial
    // one flushed on stop. Each is the level it averaged.
    expect(samples).toHaveLength(Math.ceil(345 * 128 * 16_000 / 44_100));
    expect(new Set(samples)).toEqual(new Set([Math.round(0.5 * 32_767)]));
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

describe('voice capture ownership', () => {
  // One lock manager shared by independently loaded JS realms. Callbacks are
  // asynchronous, as in Web Locks; stealing rejects the previous request.
  const installLocks = () => {
    let current: { reject: (error: unknown) => void } | null = null;
    const locks = {
      request: vi.fn((_name: string, options: { steal?: boolean }, callback: () => Promise<void>) =>
        new Promise<void>((resolve, reject) => {
          queueMicrotask(() => {
            if (options.steal) current?.reject(new DOMException('lock stolen', 'AbortError'));
            const owner = { reject };
            current = owner;
            void callback().then(() => {
              if (current === owner) current = null;
              resolve();
            }, reject);
          });
        })),
    };
    Object.defineProperty(navigator, 'locks', { configurable: true, value: locks });
    return locks;
  };

  const anotherRealm = async () => {
    vi.resetModules();
    return import('./voiceRecording');
  };

  it('preserves captured samples when another realm steals the lock', async () => {
    installLocks();
    const main = await anotherRealm();
    const pet = await anotherRealm();
    const recording = setup();
    await recording.pipeline.start();
    recording.capture().emit(123, 456);
    const finish = vi.fn(() => recording.pipeline.finish());
    const oldOwner = main.claimVoiceCapture(finish);
    await oldOwner.ready;
    const newOwner = pet.claimVoiceCapture(vi.fn());
    await newOwner.ready;
    await vi.waitFor(() => expect(finish).toHaveBeenCalledOnce());
    expect(oldOwner.isCurrent()).toBe(false);
    expect(newOwner.isCurrent()).toBe(true);
    recording.capture().settle();
    expect(recording.onSegment).toHaveBeenCalledOnce();
    expect(await wavSamples(recording.onSegment.mock.calls[0][0])).toEqual([123, 456]);
    oldOwner.release();
    expect(newOwner.isCurrent()).toBe(true);
    newOwner.release();
  });

  it('orders simultaneous independent-realm claims with the newest owning capture', async () => {
    installLocks();
    const main = await anotherRealm();
    const pet = await anotherRealm();
    const finishMain = vi.fn();
    const first = main.claimVoiceCapture(finishMain);
    const last = pet.claimVoiceCapture(vi.fn());
    await Promise.allSettled([first.ready, last.ready]);
    await vi.waitFor(() => expect(finishMain).toHaveBeenCalledOnce());
    expect(first.isCurrent()).toBe(false);
    expect(last.isCurrent()).toBe(true);
    first.release();
    expect(last.isCurrent()).toBe(true);
    last.release();
  });

  it('finishes the previous surface and ignores its stale release', () => {
    const finishChat = vi.fn();
    const finishShowPage = vi.fn();
    const chat = claimVoiceCapture(finishChat);
    const showPage = claimVoiceCapture(finishShowPage);

    expect(finishChat).toHaveBeenCalledOnce();
    expect(chat.isCurrent()).toBe(false);
    expect(showPage.isCurrent()).toBe(true);

    chat.release();
    expect(showPage.isCurrent()).toBe(true);

    showPage.release();
    const next = claimVoiceCapture(vi.fn());
    expect(finishShowPage).not.toHaveBeenCalled();
    next.release();
  });

  it('keeps local replacement semantics when Web Locks is unavailable', () => {
    const previousDescriptor = Object.getOwnPropertyDescriptor(navigator, 'locks');
    Reflect.deleteProperty(navigator, 'locks');
    const previousFinish = vi.fn();
    const nextFinish = vi.fn();
    const previous = claimVoiceCapture(previousFinish);
    const next = claimVoiceCapture(nextFinish);

    expect(previous.isCurrent()).toBe(false);
    expect(next.isCurrent()).toBe(true);
    expect(previousFinish).toHaveBeenCalledOnce();
    previous.release();
    expect(next.isCurrent()).toBe(true);

    next.release();
    expect(next.isCurrent()).toBe(false);
    if (previousDescriptor) Object.defineProperty(navigator, 'locks', previousDescriptor);
  });

  it('finishes an old owner through the Web Lock steal path without double finishing', async () => {
    installLocks();
    const previousFinish = vi.fn();
    const nextFinish = vi.fn();
    const previous = claimVoiceCapture(previousFinish);
    await previous.ready;
    const next = claimVoiceCapture(nextFinish);

    await expect(next.ready).resolves.toBeUndefined();
    await Promise.resolve();
    expect(previousFinish).toHaveBeenCalledOnce();
    expect(next.isCurrent()).toBe(true);
    next.release();
  });

  it('makes the newest simultaneous claim the owner', async () => {
    installLocks();
    const firstFinish = vi.fn();
    const secondFinish = vi.fn();
    const first = claimVoiceCapture(firstFinish);
    const second = claimVoiceCapture(secondFinish);

    await expect(second.ready).resolves.toBeUndefined();
    await Promise.resolve();
    expect(first.isCurrent()).toBe(false);
    expect(second.isCurrent()).toBe(true);
    expect(firstFinish).toHaveBeenCalledOnce();
    second.release();
  });
});

describe('isVoiceControlDisabled', () => {
  it('requires explicit discard before a retained recording can be replaced', () => {
    expect(isVoiceControlDisabled(false, false, false, true)).toBe(true);
    expect(isVoiceControlDisabled(false, true, false, true)).toBe(false);
  });
});
