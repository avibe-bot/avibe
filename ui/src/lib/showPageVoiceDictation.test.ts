import { describe, expect, it, vi } from 'vitest';

import { ShowPageVoiceDictation } from './showPageVoiceDictation';
import { claimVoiceCapture, type VoiceRecordingPipelineOptions } from './voiceRecording';
import type { VoiceRealtimeOptions } from './voiceRealtime';
import { VoiceTranscriptionError, type VoiceTranscriptionSegment } from './voiceTranscription';

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
      transcribeSegments: async () => undefined,
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
});
