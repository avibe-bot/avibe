// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { setLocalSnapshotPrincipal } from './localSnapshots';
import { ShowPageVoiceDictation, VOICE_LIVE_SILENT_SPEECH_MS } from './showPageVoiceDictation';
import { VOICE_LEVEL_INTERVAL_MS, type VoiceRecordingPipelineOptions } from './voiceRecording';
import type { VoiceRealtimeOptions } from './voiceRealtime';
import { deleteVoiceTake, isVoiceTakeLive, readVoiceTakeAudio, readVoiceTakes } from './voiceTakeStore';
import { VoiceTranscriptionError } from './voiceTranscription';

// A dictation that keeps its take on the device: a reload, a crash, or a surface that goes away
// loses none of what was said.

const idle = () => new Promise((resolve) => setTimeout(resolve, 30));

const harness = (options: {
  finalize?: ReturnType<typeof vi.fn>;
  realtimeFinish?: () => Promise<{ text: string; cleanup: 'success' | 'fallback' }>;
  onLiveChange?: (live: boolean) => void;
} = {}) => {
  let pipeline!: VoiceRecordingPipelineOptions;
  let realtime!: VoiceRealtimeOptions;
  const realtimeAbort = vi.fn();
  const finish = vi.fn(() => {
    pipeline.onSegment(new Blob(['tail'], { type: 'audio/wav' }), { durationMs: 20_000, overlapMs: 500, final: true });
    pipeline.onStopped('finish', { pendingSegmentCount: 1 });
  });
  const enqueued: number[] = [];
  const dictation = new ShowPageVoiceDictation({
    before: '',
    after: '',
    keep: { target: 'ses_a', intent: 'send', label: 'To Vibey' },
    onLiveChange: options.onLiveChange,
  }, {
    getUserMedia: async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream,
    createRealtime: (realtimeOptions) => {
      realtime = realtimeOptions;
      return {
        start: async () => undefined,
        sendPcm: vi.fn(() => true),
        finish: options.realtimeFinish ?? (async () => { throw new Error('realtime unavailable'); }),
        abort: realtimeAbort,
      };
    },
    createQueue: () => ({
      enqueue: async (segment) => {
        enqueued.push(segment.sequence);
      },
    }),
    createPipeline: (pipelineOptions) => {
      pipeline = pipelineOptions;
      return { start: async () => true, abort: vi.fn(), finish };
    },
    finalize: options.finalize ?? vi.fn(async () => ({ text: 'every word', cleanup: 'success' as const })),
    newDictationId: () => 'kept-take',
  });
  return {
    dictation,
    finish,
    realtimeAbort,
    enqueued,
    pipeline: () => pipeline,
    realtime: () => realtime,
  };
};

afterEach(async () => {
  for (const take of await readVoiceTakes()) await deleteVoiceTake(take.id);
  setLocalSnapshotPrincipal(null);
});

describe('ShowPageVoiceDictation keeps its take', () => {
  it('stores each segment as it is recorded, keeps the text until it lands, then forgets it', async () => {
    setLocalSnapshotPrincipal('local');
    const take = harness();
    await take.dictation.start();
    take.pipeline().onSegment(new Blob(['minute'], { type: 'audio/wav' }), { durationMs: 60_000, final: false });
    await vi.waitFor(async () => {
      expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ id: 'kept-take', status: 'recording' });
      expect(await (await readVoiceTakeAudio('kept-take')).get(0)?.text()).toBe('minute');
    });

    take.dictation.finish();
    await expect(take.dictation.done).resolves.toBe('every word');
    await vi.waitFor(async () => {
      expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ status: 'transcribed', text: 'every word' });
    });

    take.dictation.landed();
    await vi.waitFor(async () => expect(await readVoiceTakes()).toEqual([]));
  });

  it('keeps a take that could not be transcribed, with its audio, for later', async () => {
    setLocalSnapshotPrincipal('local');
    const take = harness({ finalize: vi.fn().mockRejectedValue(new VoiceTranscriptionError('unavailable')) });
    await take.dictation.start();
    take.dictation.finish();
    await expect(take.dictation.done).rejects.toMatchObject({ code: 'unavailable' });
    await vi.waitFor(async () => {
      expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ status: 'pending' });
      expect(await (await readVoiceTakeAudio('kept-take')).get(0)?.text()).toBe('tail');
    });
  });

  it('finishes a take whose surface went away, and leaves it on the device for the pending items', async () => {
    setLocalSnapshotPrincipal('local');
    const take = harness({ finalize: vi.fn().mockRejectedValue(new VoiceTranscriptionError('unavailable')) });
    await take.dictation.start();
    expect(isVoiceTakeLive('kept-take')).toBe(true);

    take.dictation.detach();
    expect(take.finish).toHaveBeenCalledOnce();
    await take.dictation.done.catch(() => undefined);
    await vi.waitFor(async () => {
      expect(isVoiceTakeLive('kept-take')).toBe(false);
      expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ status: 'pending' });
    });
  });

  it('forgets a take the reader discards', async () => {
    setLocalSnapshotPrincipal('local');
    const take = harness();
    await take.dictation.start();
    take.pipeline().onSegment(new Blob(['minute'], { type: 'audio/wav' }), { durationMs: 60_000, final: false });
    take.dictation.abort();
    await idle();
    await vi.waitFor(async () => expect(await readVoiceTakes()).toEqual([]));
  });
});

describe('ShowPageVoiceDictation says when live recognition is not working', () => {
  const speak = (pipeline: VoiceRecordingPipelineOptions, ms: number, rms = 0.05) => {
    for (let elapsed = 0; elapsed < ms; elapsed += VOICE_LEVEL_INTERVAL_MS) pipeline.onLevel?.(rms);
  };

  it('takes speech that brings no live preview as a realtime path that is not working, and goes on over HTTP', async () => {
    setLocalSnapshotPrincipal(null);
    const onLiveChange = vi.fn();
    const take = harness({ onLiveChange });
    await take.dictation.start();
    take.pipeline().onSegment(new Blob(['minute'], { type: 'audio/wav' }), { durationMs: 60_000, final: false });
    expect(take.enqueued).toEqual([]);

    speak(take.pipeline(), VOICE_LIVE_SILENT_SPEECH_MS - 500);
    expect(onLiveChange).not.toHaveBeenCalled();
    speak(take.pipeline(), 600);
    expect(onLiveChange).toHaveBeenCalledWith(false);
    expect(take.realtimeAbort).toHaveBeenCalled();
    expect(take.enqueued).toEqual([0]);
  });

  it('keeps waiting on a realtime path whose previews come, and through silence', async () => {
    setLocalSnapshotPrincipal(null);
    const onLiveChange = vi.fn();
    const take = harness({ onLiveChange });
    await take.dictation.start();
    for (let second = 0; second < 10; second += 1) {
      speak(take.pipeline(), 1_000);
      take.realtime().onPreview?.({ text: 'words', stash: '' });
    }
    speak(take.pipeline(), 30_000, 0.005);
    expect(onLiveChange).not.toHaveBeenCalled();
    expect(take.realtimeAbort).not.toHaveBeenCalled();
  });

  it('says so at once when the realtime path fails', async () => {
    setLocalSnapshotPrincipal(null);
    const onLiveChange = vi.fn();
    const take = harness({ onLiveChange });
    await take.dictation.start();
    take.realtime().onError?.(new Error('realtime_closed'));
    expect(onLiveChange).toHaveBeenCalledWith(false);
  });
});
