// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { createElement } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { PendingVoiceTakes } from '../components/voice/PendingVoiceTakes';
import { noteVoiceStatus } from './voiceConsent';
import { ShowPageVoiceHost, ANNOTATION_VOICE_REQUEST_MESSAGE, type ShowPageVoiceEvent } from './showPageVoiceBridge';
import * as transcription from './voiceTranscription';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setLocalSnapshotPrincipal } from './localSnapshots';
import { ShowPageVoiceDictation, type ShowPageVoiceDictationInput } from './showPageVoiceDictation';
import { VOICE_LEVEL_INTERVAL_MS, type VoiceRecordingPipelineOptions } from './voiceRecording';
import type { VoiceRealtimeOptions } from './voiceRealtime';
import { deleteVoiceTake, isVoiceTakeLive, readVoiceTakeAudio, readVoiceTakes } from './voiceTakeStore';
import { finalizeVoiceDictation, VoiceTranscriptionError } from './voiceTranscription';

// A dictation that keeps its take on the device: a reload, a crash, or a surface that goes away
// loses none of what was said.

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const idle = () => new Promise((resolve) => setTimeout(resolve, 30));
const testDictations = new Set<ShowPageVoiceDictation>();

const harness = (options: {
  input?: Partial<ShowPageVoiceDictationInput>;
  finalize?: ReturnType<typeof vi.fn>;
  realtimeFinish?: () => Promise<{ text: string; cleanup: 'success' | 'fallback' }>;
  onLiveChange?: (live: boolean) => void;
  /** Finishing writes the last segment and stops at once; false leaves it to the test. */
  stopOnFinish?: boolean;
} = {}) => {
  let pipeline!: VoiceRecordingPipelineOptions;
  let realtime!: VoiceRealtimeOptions;
  const realtimeAbort = vi.fn();
  const trackStop = vi.fn();
  const pipelineAbort = vi.fn();
  const stop = () => {
    pipeline.onSegment(new Blob(['tail'], { type: 'audio/wav' }), { durationMs: 20_000, overlapMs: 500, final: true });
    pipeline.onStopped('finish', { pendingSegmentCount: 1 });
  };
  const finish = vi.fn(() => {
    if (options.stopOnFinish ?? true) stop();
  });
  const enqueued: number[] = [];
  const dictation = new ShowPageVoiceDictation({
    before: '',
    after: '',
    keep: { target: 'ses_a', intent: 'send' },
    onLiveChange: options.onLiveChange,
    ...options.input,
  }, {
    getUserMedia: async () => ({ getTracks: () => [{ stop: trackStop }] }) as unknown as MediaStream,
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
      return { start: async () => true, abort: pipelineAbort, finish };
    },
    finalize: options.finalize ?? vi.fn(async () => ({ text: 'every word', cleanup: 'success' as const })),
    newDictationId: () => 'kept-take',
  });
  testDictations.add(dictation);
  return {
    dictation,
    finish,
    stop,
    realtimeAbort,
    trackStop,
    pipelineAbort,
    enqueued,
    pipeline: () => pipeline,
    realtime: () => realtime,
  };
};

afterEach(async () => {
  cleanup();
  for (const dictation of testDictations) {
    void dictation.done.catch(() => undefined);
    dictation.abort();
  }
  testDictations.clear();
  await idle();
  vi.restoreAllMocks();
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

// The reader stops waiting for a take's words (the card's × while it is finalized): nothing more is
// asked of the network, nothing lands, and what was said is kept only when it was worth saying.
describe('ShowPageVoiceDictation set aside', () => {
  /** One second of the microphone at a level: speech, or a quiet room. */
  const second = (amplitude: number) => new Int16Array(16_000).map((_, index) => (index % 2 ? amplitude : -amplitude));
  const SPEECH = 3_000;
  const SILENCE = 0;
  /** A finalization that waits on the network until its signal aborts. */
  const slowFinalize = () => vi.fn((...[, , dependencies]: Parameters<typeof finalizeVoiceDictation>) => new Promise<never>((_, reject) => {
    dependencies?.signal?.addEventListener('abort', () => reject(new VoiceTranscriptionError('cancelled')));
  }));

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Records `ms` of `amplitude`, as the clock and the recorder see it. */
  const record = (take: ReturnType<typeof harness>, ms: number, amplitude: number) => {
    for (let elapsed = 0; elapsed < ms; elapsed += 1_000) take.pipeline().onPcm?.(second(amplitude));
    vi.setSystemTime(Date.now() + ms);
  };

  it('stops the request it waits on and keeps a take that held speech for 3 s, for the reader', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    setLocalSnapshotPrincipal('local');
    const finalize = slowFinalize();
    const take = harness({ finalize });
    await take.dictation.start();
    record(take, 5_000, SPEECH);
    take.dictation.finish();
    await vi.waitFor(() => expect(finalize).toHaveBeenCalledOnce());
    const signal = finalize.mock.calls[0]![2]!.signal!;

    const kept = take.dictation.setAside();
    expect(signal.aborted).toBe(true);
    expect(take.realtimeAbort).toHaveBeenCalled();
    await expect(kept).resolves.toBe(true);
    await expect(take.dictation.done).rejects.toMatchObject({ code: 'cancelled' });
    // On the device, with its audio, for the pending voice items: no surface holds it, and it is
    // marked so that no page tries it again on its own as it first shows it.
    await vi.waitFor(async () => {
      expect(isVoiceTakeLive('kept-take')).toBe(false);
      expect((await readVoiceTakes('ses_a'))[0]).toMatchObject({ id: 'kept-take', status: 'pending', setAside: true });
      expect(await (await readVoiceTakeAudio('kept-take')).get(0)?.text()).toBe('tail');
    });
    // A discard meant for the surface's own copy does not reach the kept take.
    take.dictation.abort();
    await idle();
    expect(await readVoiceTakes('ses_a')).toHaveLength(1);
  });

  it.each([
    { said: 'speech for 5 s', ms: 5_000, amplitude: SPEECH, tail: SPEECH, kept: true },
    { said: 'speech for 2 s', ms: 2_000, amplitude: SPEECH, tail: SPEECH, kept: false },
    { said: 'silence for 5 s', ms: 5_000, amplitude: SILENCE, tail: SILENCE, kept: false },
    // The audio the recorder flushes as it stops is judged with the rest.
    { said: 'silence for 5 s, then speech in its last audio', ms: 5_000, amplitude: SILENCE, tail: SPEECH, kept: true },
  ])('set aside while its last segment is still being written, $said is kept: $kept', async ({ ms, amplitude, tail, kept }) => {
    vi.useFakeTimers({ toFake: ['Date'] });
    setLocalSnapshotPrincipal('local');
    const finalize = vi.fn();
    const take = harness({ finalize, stopOnFinish: false });
    await take.dictation.start();
    record(take, ms, amplitude);
    take.dictation.finish();

    const decided = take.dictation.setAside();
    take.pipeline().onPcm?.(second(tail));
    take.stop();
    await expect(decided).resolves.toBe(kept);
    await expect(take.dictation.done).rejects.toMatchObject({ code: 'cancelled' });
    await idle();
    await vi.waitFor(async () => {
      expect(isVoiceTakeLive('kept-take')).toBe(false);
      const takes = await readVoiceTakes('ses_a');
      if (!kept) {
        expect(takes).toEqual([]);
        return;
      }
      expect(takes[0]).toMatchObject({ status: 'pending' });
      expect(await (await readVoiceTakeAudio('kept-take')).get(0)?.text()).toBe('tail');
    });
    // Nothing is asked of recognition once the reader stopped waiting.
    expect(finalize).not.toHaveBeenCalled();
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

    // Four seconds of speech with no preview: not after three and a half, but after four.
    speak(take.pipeline(), 3_500);
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

describe('native App Lock discards only unfinished dictation', () => {
  let revision = 20000;
  it('keeps a disposed retry owner until its result reaches the same pending take', async () => {
    const signal = (locked: boolean) => act(() => {
      Object.assign(window, { __AVIBE_VOICE_LOCK__: { revision: ++revision, locked } });
      window.dispatchEvent(new Event('avibe:voice-lock'));
    });
    setLocalSnapshotPrincipal('local'); noteVoiceStatus({ consent: 'granted' });
    let finishRetry!: (result: { text: string; cleanup: 'success' }) => void;
    const finalize = vi.fn()
      .mockRejectedValueOnce(new VoiceTranscriptionError('unavailable'))
      .mockImplementationOnce(() => new Promise((resolve) => { finishRetry = resolve; }));
    const pendingRetry = vi.spyOn(transcription, 'finalizeVoiceDictation');
    let take!: ReturnType<typeof harness>;
    const events: ShowPageVoiceEvent[] = [];
    const host = new ShowPageVoiceHost({ post: (event) => events.push(event),
      availability: async () => ({ available: true, maxFileBytes: null }),
      keep: () => ({ target: 'ses_a', intent: 'insert' }),
      createSession: (input) => {
        take = harness({ input, finalize });
        return take.dictation;
      } });
    const request = { type: ANNOTATION_VOICE_REQUEST_MESSAGE, action: 'start', requestId: 'retry-dispose', before: '', after: '' };
    try {
      signal(false); host.handle(request);
      await vi.waitFor(() => expect(events.some((event) => event.kind === 'started')).toBe(true));
      take.dictation.finish();
      await vi.waitFor(() => expect(events.at(-1)).toMatchObject({ retryable: true }));
      host.handle({ ...request, action: 'retry' });
      await vi.waitFor(() => expect(finalize).toHaveBeenCalledTimes(2));
      signal(true); host.dispose(); host.dispose();
      const insert = vi.fn(() => true); const send = vi.fn(async () => true);
      render(createElement(PendingVoiceTakes, { target: 'ses_a', onInsert: insert, onSend: send }));
      for (const locked of [true, false]) {
        signal(locked);
        fireEvent(window, new Event('online')); fireEvent(document, new Event('visibilitychange'));
        await act(idle);
        expect(isVoiceTakeLive('kept-take')).toBe(true);
        expect(pendingRetry).not.toHaveBeenCalled();
      }
      signal(true);
      await act(async () => finishRetry({ text: 'one preserved result', cleanup: 'success' }));
      const button = await screen.findByRole('button', { name: 'voice.pending.insert' });
      expect(events.some((event) => event.kind === 'result')).toBe(false);
      expect(pendingRetry).not.toHaveBeenCalled(); expect(finalize).toHaveBeenCalledTimes(2);
      fireEvent.click(button); expect(insert).not.toHaveBeenCalled();
      signal(false); fireEvent.click(button);
      await vi.waitFor(() => expect(insert).toHaveBeenCalledExactlyOnceWith('one preserved result'));
      expect(send).not.toHaveBeenCalled(); expect(pendingRetry).not.toHaveBeenCalled();
      await vi.waitFor(async () => expect(await readVoiceTakes()).toEqual([]));
    } finally { host.dispose(); Reflect.deleteProperty(window, '__AVIBE_VOICE_LOCK__'); }
  });

  it('disposes a settled Show owner into visible pending audio without retrying while locked or on reload', async () => {
    const signal = (locked: boolean) => act(() => {
      Object.assign(window, { __AVIBE_VOICE_LOCK__: { revision: ++revision, locked } });
      window.dispatchEvent(new Event('avibe:voice-lock'));
    });
    setLocalSnapshotPrincipal('local'); noteVoiceStatus({ consent: 'granted' });
    const retry = vi.spyOn(transcription, 'finalizeVoiceDictation').mockResolvedValue({ text: 'recovered from pending', cleanup: 'success' });
    let take!: ReturnType<typeof harness>;
    const events: ShowPageVoiceEvent[] = [];
    const host = new ShowPageVoiceHost({ post: (event) => events.push(event),
      availability: async () => ({ available: true, maxFileBytes: null }),
      keep: () => ({ target: 'ses_a', intent: 'insert' }),
      createSession: (input) => {
        take = harness({ input, finalize: vi.fn().mockRejectedValue(new VoiceTranscriptionError('unavailable')) });
        return take.dictation;
      } });
    try {
      signal(false);
      host.handle({ type: ANNOTATION_VOICE_REQUEST_MESSAGE, action: 'start', requestId: 'kept', before: '', after: '' });
      await vi.waitFor(() => expect(events.some((event) => event.kind === 'started')).toBe(true));
      take.dictation.finish();
      await vi.waitFor(() => expect(events.at(-1)).toMatchObject({ retryable: true }));
      signal(true); host.dispose(); host.dispose();
      await vi.waitFor(() => expect(isVoiceTakeLive('kept-take')).toBe(false));
      const insert = vi.fn(() => true); const send = vi.fn(async () => true);
      const pending = () => render(createElement(PendingVoiceTakes, { target: 'ses_a', onInsert: insert, onSend: send }));
      let view = pending();
      await screen.findByRole('button', { name: 'voice.retry' });
      for (let reload = 0; reload < 2; reload++) {
        fireEvent(window, new Event('online')); fireEvent(document, new Event('visibilitychange'));
        await act(idle); expect(retry).not.toHaveBeenCalled();
        view.unmount(); view = pending();
        await screen.findByRole('button', { name: 'voice.retry' });
      }
      expect(await readVoiceTakeAudio('kept-take')).toHaveProperty('size', 1);
      signal(false); fireEvent(window, new Event('online'));
      const button = await screen.findByRole('button', { name: 'voice.pending.insert' });
      expect(retry).toHaveBeenCalledOnce(); expect(insert).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled();
      fireEvent.click(button);
      await vi.waitFor(() => expect(insert).toHaveBeenCalledExactlyOnceWith('recovered from pending'));
      expect(send).not.toHaveBeenCalled(); expect(retry).toHaveBeenCalledOnce();
      await vi.waitFor(async () => expect(await readVoiceTakes()).toEqual([]));
    } finally { host.dispose(); Reflect.deleteProperty(window, '__AVIBE_VOICE_LOCK__'); }
  });

  it.each(['listening', 'finishing', 'detached'] as const)('aborts %s resources and rejects late segments without keeping a take', async (phase) => {
    setLocalSnapshotPrincipal('local');
    const take = harness({ realtimeFinish: () => new Promise(() => {}) });
    try {
      await take.dictation.start();
      take.pipeline().onSegment(new Blob(['before']), { durationMs: 2000, final: false });
      await vi.waitFor(async () => expect((await readVoiceTakes()).length).toBe(1));
      if (phase === 'finishing') take.dictation.finish();
      if (phase === 'detached') take.dictation.detach();
      Object.assign(window, { __AVIBE_VOICE_LOCK__: { revision: ++revision, locked: true } });
      window.dispatchEvent(new Event('avibe:voice-lock'));
      await expect(take.dictation.done).rejects.toMatchObject({ code: 'cancelled' });
      expect(take.trackStop).toHaveBeenCalled(); expect(take.pipelineAbort).toHaveBeenCalled();
      expect(take.realtimeAbort).toHaveBeenCalled();
      take.pipeline().onSegment(new Blob(['after']), { durationMs: 2000, final: true });
      take.pipeline().onStopped('finish', { pendingSegmentCount: 1 });
      await vi.waitFor(async () => expect(await readVoiceTakes()).toEqual([]));
    } finally { Reflect.deleteProperty(window, '__AVIBE_VOICE_LOCK__'); }
  });
});
