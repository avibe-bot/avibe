import { describe, expect, it, vi } from 'vitest';

import { ShowPageVoiceDictation } from './showPageVoiceDictation';
import { claimVoiceCapture, isVoiceCaptureActive, type VoiceRecordingPipelineOptions } from './voiceRecording';
import type { VoiceRealtimeOptions } from './voiceRealtime';

const telemetry = vi.hoisted(() => ({ emit: vi.fn() }));
vi.mock('./voiceTelemetry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./voiceTelemetry')>()),
  emitVoiceTelemetry: telemetry.emit,
}));
import { VoiceTranscriptionError, type VoiceTranscriptionSegment } from './voiceTranscription';

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
};

const deferred = <T>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
};

/** `ms` of the recorder's 16 kHz PCM at a steady loudness (RMS, 0–1). */
const pcm = (rms: number, ms: number) => new Int16Array(Math.round(16 * ms)).fill(Math.round(rms * 32_767));

const fakeStream = () => {
  const stop = vi.fn();
  return {
    stream: { getTracks: () => [{ stop }] } as unknown as MediaStream,
    stop,
  };
};

describe('ShowPageVoiceDictation', () => {
  it('uses the existing realtime pipeline without an aggregate recording limit', async () => {
    const { stream } = fakeStream();
    let pipelineOptions: VoiceRecordingPipelineOptions | null = null;
    let realtimeOptions: VoiceRealtimeOptions | null = null;
    const realtimeFinish = vi.fn(async () => ({
      text: 'final realtime words',
      cleanup: 'success' as const,
    }));
    const dictation = new ShowPageVoiceDictation({
      before: 'before',
      after: 'after',
      maxFileBytes: null,
    }, {
      getUserMedia: async () => stream,
      createRealtime: (options) => {
        realtimeOptions = options;
        return {
          start: async () => undefined,
          sendPcm: vi.fn(() => true),
          finish: realtimeFinish,
          abort: vi.fn(),
        };
      },
      createPipeline: (options) => {
        pipelineOptions = options;
        return {
          start: async () => true,
          abort: vi.fn(),
          finish: () => {
            options.onSegment(new Blob(['tail'], { type: 'audio/wav' }), {
              durationMs: 12_000,
              final: true,
            });
            options.onStopped('finish', { pendingSegmentCount: 1 });
          },
        };
      },
      createQueue: () => ({ enqueue: async () => undefined }),
      newDictationId: () => 'dictation-1',
    });

    await dictation.start();
    expect(pipelineOptions).toMatchObject({ segmentMs: 60_000, maxFileBytes: null });
    expect(realtimeOptions).toMatchObject({ before: 'before', after: 'after' });

    // Internal minute-sized segments may repeat indefinitely; none finishes the
    // user recording. Only the explicit finish below is terminal.
    pipelineOptions!.onSegment(new Blob(['minute-1'], { type: 'audio/wav' }), {
      durationMs: 60_000,
    });
    pipelineOptions!.onSegment(new Blob(['minute-2'], { type: 'audio/wav' }), {
      durationMs: 60_000,
      overlapMs: 500,
    });
    let settled = false;
    void dictation.done.finally(() => { settled = true; });
    await Promise.resolve();
    expect(settled).toBe(false);

    dictation.finish();
    await expect(dictation.done).resolves.toBe('final realtime words');
    expect(realtimeFinish).toHaveBeenCalledOnce();
  });

  it('finishes capture when another client surface takes microphone ownership', async () => {
    const { stream } = fakeStream();
    const finish = vi.fn();
    const abort = vi.fn();
    const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
      getUserMedia: async () => stream,
      createRealtime: () => ({
        start: async () => undefined,
        sendPcm: vi.fn(() => true),
        finish: async () => ({ text: 'done', cleanup: 'success' as const }),
        abort: vi.fn(),
      }),
      createPipeline: () => ({ start: async () => true, finish, abort }),
      createQueue: () => ({ enqueue: async () => undefined }),
    });

    await dictation.start();
    const nextSurface = claimVoiceCapture(vi.fn());

    expect(finish).toHaveBeenCalledOnce();
    nextSurface.release();
    dictation.abort();
    expect(abort).toHaveBeenCalledOnce();
  });

  it('logs a take whose microphone never opened, and why', async () => {
    telemetry.emit.mockReset();
    const createPipeline = vi.fn();
    const dictation = new ShowPageVoiceDictation({ before: '', after: '', entry: 'hold' }, {
      // On iOS another app holding the microphone refuses it so.
      getUserMedia: async () => {
        throw new DOMException('The I/O read operation failed.', 'NotReadableError');
      },
      createPipeline,
      createQueue: () => ({ enqueue: async () => undefined }),
    });

    await expect(dictation.start()).rejects.toMatchObject({ name: 'NotReadableError' });

    expect(createPipeline).not.toHaveBeenCalled();
    expect(dictation.captureClass()).toBe('no_microphone');
    expect(telemetry.emit).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      event: 'capture_trace',
      outcome: 'failed',
      dictationId: dictation.id,
      entry: 'hold',
      captureClass: 'no_microphone',
      mic: 'NotReadableError',
      micMs: expect.any(Number),
      attempts: [],
    }));
    // The browser's message is not what it logs.
    expect(JSON.stringify(telemetry.emit.mock.calls)).not.toContain('I/O read');
  });

  it('records its capture in the same trace it reads its microphone from', async () => {
    const { stream } = fakeStream();
    let options: VoiceRecordingPipelineOptions | undefined;
    const dictation = new ShowPageVoiceDictation({ before: '', after: '', entry: 'hold' }, {
      getUserMedia: async () => stream,
      createRealtime: () => ({
        start: async () => undefined,
        sendPcm: vi.fn(() => true),
        finish: async () => ({ text: '', cleanup: 'success' as const }),
        abort: vi.fn(),
      }),
      createPipeline: (pipelineOptions) => {
        options = pipelineOptions;
        return { start: async () => true, finish: vi.fn(), abort: vi.fn() };
      },
      createQueue: () => ({ enqueue: async () => undefined }),
    });

    await dictation.start();
    // What the pipeline records there is what the hold reads back.
    options?.trace?.attempt('initial', stream);
    options?.trace?.observePcm(new Int16Array(2048).fill(8000));
    expect(options?.trace).toMatchObject({ entry: 'hold', dictationId: dictation.id });
    expect(dictation.captureClass()).toBe('sound');
    dictation.abort();
  });

  it('leaves live recognition when its take is recorded without Web Audio, and says how it records', async () => {
    const { stream } = fakeStream();
    let options: VoiceRecordingPipelineOptions | undefined;
    const realtimeAbort = vi.fn();
    const onLiveChange = vi.fn();
    const onCaptureState = vi.fn();
    const dictation = new ShowPageVoiceDictation({ before: '', after: '', entry: 'hold', onLiveChange, onCaptureState }, {
      getUserMedia: async () => stream,
      createRealtime: () => ({
        start: async () => undefined,
        sendPcm: vi.fn(() => true),
        finish: async () => ({ text: '', cleanup: 'success' as const }),
        abort: realtimeAbort,
      }),
      createPipeline: (pipelineOptions) => {
        options = pipelineOptions;
        return { start: async () => true, finish: vi.fn(), abort: vi.fn() };
      },
      createQueue: () => ({ enqueue: async () => undefined }),
    });
    await dictation.start();

    options?.onCaptureState?.('recorder');
    expect(realtimeAbort).toHaveBeenCalledOnce();
    expect(onLiveChange).toHaveBeenCalledWith(false);
    options?.onCaptureState?.('no_sound');
    expect(onCaptureState.mock.calls).toEqual([['recorder'], ['no_sound']]);
    // Live recognition is left once.
    expect(realtimeAbort).toHaveBeenCalledOnce();
    dictation.abort();
  });

  it('closes a microphone granted after the take was cancelled, and frees the capture', async () => {
    // A permission alert can answer after the press that asked has ended.
    const { stream, stop } = fakeStream();
    const granted = deferred<MediaStream>();
    const createPipeline = vi.fn();
    const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
      getUserMedia: () => granted.promise,
      createPipeline,
      createQueue: () => ({ enqueue: async () => undefined }),
    });

    const starting = dictation.start();
    await Promise.resolve();
    expect(isVoiceCaptureActive()).toBe(true);
    dictation.abort();
    expect(isVoiceCaptureActive()).toBe(false);
    granted.resolve(stream);

    await expect(starting).rejects.toMatchObject({ code: 'cancelled' });
    expect(stop).toHaveBeenCalled();
    expect(createPipeline).not.toHaveBeenCalled();
    expect(isVoiceCaptureActive()).toBe(false);
    // And the take is still logged, once.
    expect(telemetry.emit.mock.calls.filter(([event]) => event.event === 'capture_trace' && event.dictationId === dictation.id))
      .toEqual([[expect.objectContaining({ outcome: 'cancelled', mic: 'ok', attempts: [] })]]);
  });

  it('preserves finalization when ownership changes during capture startup', async () => {
    const { stream } = fakeStream();
    const captureStarted = deferred<boolean>();
    const pipelineReady = deferred<void>();
    const realtimeAbort = vi.fn();
    const pipelineFinish = vi.fn();
    const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
      getUserMedia: async () => stream,
      createRealtime: () => ({
        start: async () => undefined,
        sendPcm: vi.fn(() => true),
        finish: async () => ({ text: 'buffered words', cleanup: 'success' as const }),
        abort: realtimeAbort,
      }),
      createPipeline: (options) => {
        pipelineReady.resolve(undefined);
        return {
          start: () => captureStarted.promise,
          abort: vi.fn(),
          finish: () => {
            pipelineFinish();
            options.onSegment(new Blob(['tail'], { type: 'audio/wav' }), {
              durationMs: 2_000,
              final: true,
            });
            options.onStopped('finish', { pendingSegmentCount: 1 });
          },
        };
      },
      createQueue: () => ({ enqueue: async () => undefined }),
    });

    const starting = dictation.start();
    await pipelineReady.promise;
    const nextSurface = claimVoiceCapture(vi.fn());
    expect(pipelineFinish).toHaveBeenCalledOnce();

    captureStarted.resolve(false);
    await expect(starting).resolves.toBeUndefined();
    await expect(dictation.done).resolves.toBe('buffered words');
    expect(realtimeAbort).not.toHaveBeenCalled();
    nextSurface.release();
  });

  it('falls back through the existing segment queue and finalizer', async () => {
    const { stream } = fakeStream();
    const queued: number[] = [];
    const finalize = vi.fn(async (segments: VoiceTranscriptionSegment[]) => {
      expect(segments.map(({ sequence, final, receipt }) => ({ sequence, final, receipt }))).toEqual([
        { sequence: 0, final: false, receipt: 'receipt-0' },
        { sequence: 1, final: true, receipt: undefined },
      ]);
      return { text: 'HTTP fallback words', cleanup: 'success' as const };
    });
    const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
      getUserMedia: async () => stream,
      createRealtime: (options) => ({
        start: async () => {
          options.onError?.(new Error('realtime unavailable'));
          throw new Error('realtime unavailable');
        },
        sendPcm: vi.fn(() => false),
        finish: async () => { throw new Error('realtime unavailable'); },
        abort: vi.fn(),
      }),
      createQueue: () => ({
        enqueue: async (segment) => {
          queued.push(segment.sequence);
          segment.receipt = `receipt-${segment.sequence}`;
        },
      }),
      createPipeline: (options) => ({
        start: async () => true,
        abort: vi.fn(),
        finish: () => {
          options.onSegment(new Blob(['segment'], { type: 'audio/wav' }), {
            durationMs: 60_000,
          });
          options.onSegment(new Blob(['tail'], { type: 'audio/wav' }), {
            durationMs: 4_000,
            final: true,
          });
          options.onStopped('finish', { pendingSegmentCount: 2 });
        },
      }),
      finalize,
      newDictationId: () => 'dictation-2',
    });

    await dictation.start();
    dictation.finish();
    await expect(dictation.done).resolves.toBe('HTTP fallback words');
    expect(queued).toEqual([0]);
    expect(finalize).toHaveBeenCalledOnce();
  });

  it('hands one conversation context to realtime recognition and to its HTTP fallback', async () => {
    const { stream } = fakeStream();
    let realtimeContext: unknown;
    let queueContext: unknown;
    const finalize = vi.fn(async () => ({ text: 'HTTP words', cleanup: 'success' as const }));
    const context = deferred<{ reply: string; userPrevious: string }>();
    const dictation = new ShowPageVoiceDictation({ before: '', after: '', context: context.promise }, {
      getUserMedia: async () => stream,
      createRealtime: (options) => {
        realtimeContext = options.context;
        return {
          start: async () => {
            options.onError?.(new Error('realtime unavailable'));
            throw new Error('realtime unavailable');
          },
          sendPcm: vi.fn(() => false),
          finish: async () => { throw new Error('realtime unavailable'); },
          abort: vi.fn(),
        };
      },
      createQueue: (options) => {
        queueContext = options.context;
        return { enqueue: async () => undefined };
      },
      createPipeline: (options) => ({
        start: async () => true,
        abort: vi.fn(),
        finish: () => {
          options.onSegment(new Blob(['tail'], { type: 'audio/wav' }), { durationMs: 4_000, final: true });
          options.onStopped('finish', { pendingSegmentCount: 1 });
        },
      }),
      finalize,
    });
    context.resolve({ reply: '  先跑 make release ', userPrevious: '部署前要做什么' });

    await dictation.start();
    dictation.finish();
    await expect(dictation.done).resolves.toBe('HTTP words');

    const expected = { reply: '先跑 make release', userPrevious: '部署前要做什么' };
    await expect(realtimeContext).resolves.toEqual(expected);
    expect(queueContext).toBe(realtimeContext);
    expect((finalize.mock.calls[0] as unknown[])[2]).toMatchObject({ context: realtimeContext });
  });

  it('uses the latest draft context when retrying retained audio', async () => {
    const { stream } = fakeStream();
    const finalize = vi.fn()
      .mockRejectedValueOnce(new VoiceTranscriptionError('timeout'))
      .mockResolvedValueOnce({ text: 'recovered', cleanup: 'success' as const });
    const dictation = new ShowPageVoiceDictation({
      before: 'original before',
      after: 'original after',
    }, {
      getUserMedia: async () => stream,
      createRealtime: (options) => ({
        start: async () => {
          options.onError?.(new Error('realtime unavailable'));
          throw new Error('realtime unavailable');
        },
        sendPcm: vi.fn(() => false),
        finish: async () => { throw new Error('realtime unavailable'); },
        abort: vi.fn(),
      }),
      createQueue: () => ({ enqueue: async () => undefined }),
      createPipeline: (options) => ({
        start: async () => true,
        abort: vi.fn(),
        finish: () => {
          options.onSegment(new Blob(['tail'], { type: 'audio/wav' }), {
            durationMs: 4_000,
            final: true,
          });
          options.onStopped('finish', { pendingSegmentCount: 1 });
        },
      }),
      finalize,
    });

    await dictation.start();
    dictation.finish();
    await expect(dictation.done).rejects.toMatchObject({ code: 'timeout' });
    await expect(dictation.retry({
      before: 'latest before',
      after: 'latest after',
    })).resolves.toBe('recovered');

    expect(finalize.mock.calls.map(([, context]) => context)).toEqual([
      expect.objectContaining({ before: 'original before', after: 'original after' }),
      expect.objectContaining({ before: 'latest before', after: 'latest after' }),
    ]);
  });

  it('keeps what was recorded before the capture broke, transcribes it, and can send it again', async () => {
    const { stream } = fakeStream();
    let pipelineOptions: VoiceRecordingPipelineOptions | null = null;
    const finalize = vi.fn()
      .mockRejectedValueOnce(new VoiceTranscriptionError('unavailable'))
      .mockResolvedValueOnce({ text: 'what was said before the call', cleanup: 'success' as const });
    const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
      getUserMedia: async () => stream,
      createRealtime: () => ({
        start: async () => undefined,
        sendPcm: vi.fn(() => true),
        finish: async () => ({ text: 'unused', cleanup: 'success' as const }),
        abort: vi.fn(),
      }),
      createQueue: () => ({ enqueue: async () => undefined }),
      createPipeline: (options) => {
        pipelineOptions = options;
        return { start: async () => true, abort: vi.fn(), finish: vi.fn() };
      },
      finalize,
    });

    await dictation.start();
    pipelineOptions!.onSegment(new Blob(['minute'], { type: 'audio/wav' }), { durationMs: 60_000, final: false });
    // An incoming call ends the microphone track: the recorder flushes what it heard, then stops.
    pipelineOptions!.onError?.(new Error('voice microphone track ended unexpectedly'));
    pipelineOptions!.onSegment(new Blob(['tail'], { type: 'audio/wav' }), { durationMs: 8_000, final: true });
    pipelineOptions!.onStopped('error', { pendingSegmentCount: 1 });

    await expect(dictation.done).rejects.toMatchObject({ code: 'unavailable' });
    expect(finalize).toHaveBeenCalledOnce();
    expect(dictation.canRetry()).toBe(true);
    await expect(dictation.retry({ before: '', after: '' })).resolves.toBe('what was said before the call');
    expect((finalize.mock.calls[1] as unknown[])[0]).toHaveLength(2);
  });

  it('keeps nothing from a capture that never rendered a frame', async () => {
    const { stream } = fakeStream();
    let pipelineOptions: VoiceRecordingPipelineOptions | null = null;
    const finalize = vi.fn();
    const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
      getUserMedia: async () => stream,
      createRealtime: () => ({
        start: async () => undefined,
        sendPcm: vi.fn(() => true),
        finish: async () => ({ text: 'unused', cleanup: 'success' as const }),
        abort: vi.fn(),
      }),
      createQueue: () => ({ enqueue: async () => undefined }),
      createPipeline: (options) => {
        pipelineOptions = options;
        return { start: async () => true, abort: vi.fn(), finish: vi.fn() };
      },
      finalize,
    });

    await dictation.start();
    pipelineOptions!.onError?.(new VoiceTranscriptionError('empty'));
    pipelineOptions!.onSegment(new Blob(['silence'], { type: 'audio/wav' }), { durationMs: 1_000, final: true });
    pipelineOptions!.onStopped('error', { pendingSegmentCount: 1 });

    await expect(dictation.done).rejects.toMatchObject({ code: 'empty' });
    expect(finalize).not.toHaveBeenCalled();
    expect(dictation.canRetry()).toBe(false);
  });

  it('tells a take that held speech-loud sound from a silent one', async () => {
    const { stream } = fakeStream();
    let pipelineOptions: VoiceRecordingPipelineOptions | null = null;
    const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
      getUserMedia: async () => stream,
      createRealtime: () => ({
        start: async () => undefined,
        sendPcm: vi.fn(() => true),
        finish: async () => ({ text: '', cleanup: 'fallback' as const }),
        abort: vi.fn(),
      }),
      createQueue: () => ({ enqueue: async () => undefined }),
      createPipeline: (options) => {
        pipelineOptions = options;
        return { start: async () => true, abort: vi.fn(), finish: vi.fn() };
      },
    });

    await dictation.start();
    // A quiet room: about −55 dBFS for three seconds.
    pipelineOptions!.onPcm?.(pcm(0.0018, 3_000));
    expect(dictation.voiced()).toBe(false);
    // Soft speech: about −30 dBFS for half a second.
    pipelineOptions!.onPcm?.(pcm(0.03, 512));
    expect(dictation.voiced()).toBe(true);
    dictation.abort();
  });

  describe('the silence gate', () => {
    const gated = (realtimeFinish: () => Promise<{ text: string; cleanup: 'success' | 'fallback' }>) => {
      const { stream } = fakeStream();
      let pipelineOptions: VoiceRecordingPipelineOptions | null = null;
      let realtimeOptions: VoiceRealtimeOptions | null = null;
      const finalize = vi.fn();
      const realtimeAbort = vi.fn();
      const finish = vi.fn(() => {
        pipelineOptions!.onSegment(new Blob(['quiet'], { type: 'audio/wav' }), { durationMs: 3_000, final: true });
        pipelineOptions!.onStopped('finish', { pendingSegmentCount: 1 });
      });
      const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
        getUserMedia: async () => stream,
        createRealtime: (options) => {
          realtimeOptions = options;
          return { start: async () => undefined, sendPcm: vi.fn(() => true), finish: vi.fn(realtimeFinish), abort: realtimeAbort };
        },
        createQueue: () => ({ enqueue: async () => undefined }),
        createPipeline: (options) => {
          pipelineOptions = options;
          return { start: async () => true, abort: vi.fn(), finish };
        },
        finalize,
      });
      return { dictation, finalize, realtimeAbort, pipeline: () => pipelineOptions!, realtime: () => realtimeOptions! };
    };
    // A room with nobody speaking: about −50 dBFS for three seconds.
    const quiet = (pipeline: VoiceRecordingPipelineOptions, readings = 94) => {
      pipeline.onPcm?.(pcm(0.0032, readings * 32));
    };

    it('does not ask recognition about a take that held no speech', async () => {
      const take = gated(async () => ({ text: 'Vibe、Vibey、Avibe 语音输入。', cleanup: 'success' }));
      await take.dictation.start();
      quiet(take.pipeline());
      take.dictation.finish();

      await expect(take.dictation.done).rejects.toMatchObject({ code: 'empty' });
      expect(take.finalize).not.toHaveBeenCalled();
      expect(take.realtimeAbort).toHaveBeenCalled();
      expect(take.dictation.voiced()).toBe(false);
      expect(take.dictation.canRetry()).toBe(false);
    });

    it('judges the tail the recorder flushes after the release too', async () => {
      const take = gated(async () => ({ text: 'said at the very end', cleanup: 'success' }));
      await take.dictation.start();
      quiet(take.pipeline());
      // Speech still queued in the capture when the release came: it reaches the segment, and the gate.
      take.pipeline().onPcm?.(pcm(0.05, 400));
      take.dictation.finish();

      await expect(take.dictation.done).resolves.toBe('said at the very end');
    });

    it('trusts live words over the levels', async () => {
      const take = gated(async () => ({ text: '轻声说的一句', cleanup: 'success' }));
      await take.dictation.start();
      quiet(take.pipeline(), 30);
      take.realtime().onPreview?.({ text: '轻声', stash: '说的' });
      quiet(take.pipeline(), 30);
      take.dictation.finish();

      await expect(take.dictation.done).resolves.toBe('轻声说的一句');
    });

    it('does not judge audio this browser could not decode as silent', async () => {
      const { stream } = fakeStream();
      let pipelineOptions: VoiceRecordingPipelineOptions | null = null;
      const finalize = vi.fn()
        .mockRejectedValueOnce(new VoiceTranscriptionError('unavailable'))
        .mockResolvedValueOnce({ text: 'said in the second minute', cleanup: 'success' as const });
      const dictation = new ShowPageVoiceDictation({ before: '', after: '' }, {
        getUserMedia: async () => stream,
        createRealtime: () => ({
          start: async () => undefined,
          sendPcm: vi.fn(() => true),
          finish: async () => ({ text: '', cleanup: 'fallback' as const }),
          abort: vi.fn(),
        }),
        createQueue: () => ({ enqueue: async () => undefined }),
        createPipeline: (options) => {
          pipelineOptions = options;
          return { start: async () => true, abort: vi.fn(), finish: vi.fn() };
        },
        finalize,
      });

      await dictation.start();
      // The first recorded minute decoded to a quiet room; the second could not be decoded at all.
      quiet(pipelineOptions!);
      pipelineOptions!.onSegment(new Blob(['quiet minute'], { type: 'audio/wav' }), { durationMs: 60_000, final: false });
      expect(dictation.voiced()).toBe(false);
      pipelineOptions!.onSegment(
        new Blob(['undecodable minute'], { type: 'audio/mp4' }),
        { durationMs: 60_000, final: true, unjudged: true },
      );
      pipelineOptions!.onStopped('finish', { pendingSegmentCount: 0 });

      await expect(dictation.done).rejects.toMatchObject({ code: 'unavailable' });
      expect(finalize).toHaveBeenCalledOnce();
      expect(dictation.voiced()).toBe(true);
      expect(dictation.canRetry()).toBe(true);
      await expect(dictation.retry({ before: '', after: '' })).resolves.toBe('said in the second minute');
    });

    it('sends a take on as before when no loudness was reported', async () => {
      const take = gated(async () => ({ text: 'heard', cleanup: 'success' }));
      await take.dictation.start();
      take.dictation.finish();

      await expect(take.dictation.done).resolves.toBe('heard');
    });
  });
});

