/** @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { noteAppVoice } from './appVoice';
import {
  adoptedCaptureFactory,
  emptyVoiceStream,
  isNativeHoldGone,
  nativeCaptureAllowed,
  NativePcmCapture,
  nativeCaptureFactory,
} from './nativeVoiceCapture';
import { VoiceCaptureTrace } from './voiceCaptureTrace';
import { VoiceRecordingPipeline } from './voiceRecording';

// A take the iOS App records (ios/Avibe/NativeHoldCapture.swift), against a stand-in bridge:
// its PCM reaches the pipeline as the page's own capture's does, and its end is the take's.

type Detail = { take: string; pcm?: string; ended?: boolean; reason?: string };

let listeners: ((detail: Detail) => void)[] = [];
/** What listens to the App's word about a hold it recognised (`voice.hold`). */
let holdListeners: ((detail: Record<string, unknown>) => void)[] = [];
let call: ReturnType<typeof vi.fn>;
let startAnswer: () => Promise<unknown>;
let adoptAnswer: () => Promise<unknown>;
let permission: string;

const install = (capabilities: Record<string, number> = { voice: 1, voiceCapture: 1, voiceHold: 1 }) => {
  listeners = [];
  holdListeners = [];
  call = vi.fn(async (method: string) => {
    if (method === 'voice.captureStart') return startAnswer();
    if (method === 'voice.holdAdopt') return adoptAnswer();
    if (method === 'voice.permission') return { state: permission };
    return null;
  });
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities,
      call,
      on: (event: string, handler: (detail: Detail) => void) => {
        if (event === 'voice.hold') {
          const hold = handler as unknown as (detail: Record<string, unknown>) => void;
          holdListeners.push(hold);
          return () => {
            holdListeners = holdListeners.filter((listener) => listener !== hold);
          };
        }
        expect(event).toBe('voice.capture');
        listeners.push(handler);
        return () => {
          listeners = listeners.filter((listener) => listener !== handler);
        };
      },
    },
  });
};

const takeId = () => {
  const start = call.mock.calls.find(([method]) => method === 'voice.captureStart');
  if (!start) throw new Error('no take started');
  return (start[1] as { take: string }).take;
};
const emit = (detail: Partial<Detail>) => {
  for (const listener of [...listeners]) listener({ take: takeId(), ...detail });
};
const pcm = (samples: number[]) => {
  const bytes = new Uint8Array(new Int16Array(samples).buffer);
  return btoa(String.fromCharCode(...bytes));
};

const handlers = () => ({
  onSamples: vi.fn(),
  onLevel: vi.fn(),
  onCaptureState: vi.fn(),
  onStopped: vi.fn(),
  onError: vi.fn(),
});

beforeEach(() => {
  install();
  startAnswer = async () => ({ sampleRate: 16_000, activateMs: 40, engineMs: 30 });
  adoptAnswer = async () => ({ sampleRate: 16_000, timings: {} });
  permission = 'granted';
});
afterEach(() => {
  delete (window as { avibeNative?: unknown }).avibeNative;
  vi.useRealTimers();
});

describe('NativePcmCapture', () => {
  it("hands the pipeline the App's PCM and loudness, and stops when the App says it is done", async () => {
    const trace = new VoiceCaptureTrace('hold', 'take-1');
    const on = handlers();
    const capture = new NativePcmCapture(on, 5_000, trace);
    await capture.start();

    expect(call).toHaveBeenCalledWith('voice.captureStart', { take: expect.stringMatching(/^[a-z0-9]{2,64}$/) });
    expect(on.onCaptureState).toHaveBeenCalledWith('live');
    // One loudness reading per 32 ms (512 samples), as the page's own capture gives them.
    emit({ pcm: pcm([...new Array(512).fill(16_384), -1, 32_767, -32_768]) });
    const [samples] = on.onSamples.mock.calls[0];
    expect(Array.from(samples.slice(510))).toEqual([16_384, 16_384, -1, 32_767, -32_768]);
    expect(on.onLevel).toHaveBeenCalledTimes(1);
    expect(on.onLevel.mock.calls[0][0]).toBeCloseTo(0.5, 5);
    // Another take's audio is not this one's.
    for (const listener of listeners) listener({ take: 'someone-else', pcm: pcm([1, 2]) });
    expect(on.onSamples).toHaveBeenCalledTimes(1);

    capture.stop();
    expect(call).toHaveBeenCalledWith('voice.captureStop', { take: takeId() });
    // What the App still held comes before its end.
    emit({ pcm: pcm([7, 8]) });
    expect(on.onStopped).not.toHaveBeenCalled();
    emit({ ended: true });
    expect(on.onSamples).toHaveBeenCalledTimes(2);
    expect(on.onStopped).toHaveBeenCalledTimes(1);
    expect(on.onError).not.toHaveBeenCalled();
    expect(listeners).toHaveLength(0);

    const attempt = trace.toEvent('sent').attempts[0];
    expect(attempt).toMatchObject({ kind: 'native', mic: 'ok', sampleRate: 16_000, chunks: 2, levels: 1, end: 'rendered' });
    expect(attempt.firstFrameMs).toBeGreaterThanOrEqual(0);
    // The App's own two steps of the start it was asked for: where a slow start went.
    expect(attempt).toMatchObject({ holdActMs: 40, holdStartMs: 30 });
    expect(attempt.hold).toBeUndefined();
  });

  it('takes a hold the App recognised itself: its take, its audio since touch-down, its timings', async () => {
    const trace = new VoiceCaptureTrace('hold', 'take-1');
    const on = handlers();
    adoptAnswer = async () => ({
      sampleRate: 16_000,
      timings: { variant: 'early', route: 'Speaker', recognisedAt: 200, hapticAt: 200, liveAt: 61, firstBufferAt: 160, adoptedAt: 231, activateMs: 38, startMs: 21, cueToLive: -139 },
    });
    const capture = adoptedCaptureFactory('n0123456789abcdef')(emptyVoiceStream(), 16_000, 0, on, 5_000, trace) as NativePcmCapture;
    const starting = capture.start();
    // Its audio can come before the answer: listened for first.
    expect(listeners).toHaveLength(1);
    await starting;

    // Taken, never started: the App has had the microphone open since the finger came down.
    expect(call).toHaveBeenCalledWith('voice.holdAdopt', { take: 'n0123456789abcdef' });
    expect(call.mock.calls.some(([method]) => method === 'voice.captureStart')).toBe(false);
    expect(on.onCaptureState).toHaveBeenCalledWith('live');
    for (const listener of [...listeners]) listener({ take: 'n0123456789abcdef', pcm: pcm([5, 6, 7]) });
    expect(Array.from(on.onSamples.mock.calls[0][0])).toEqual([5, 6, 7]);

    capture.stop();
    expect(call).toHaveBeenCalledWith('voice.captureStop', { take: 'n0123456789abcdef' });
    // The App's last word on the hold comes before the take's end; another take's is not this one's.
    for (const listener of [...holdListeners]) listener({ phase: 'timings', take: 'someone-else', timings: { releasedAt: 1 } });
    for (const listener of [...holdListeners]) {
      listener({ phase: 'timings', take: 'n0123456789abcdef', timings: { variant: 'early', recognisedAt: 200, liveAt: 61, firstBufferAt: 160, adoptedAt: 231, releasedAt: 2_480, end: 'stopped' } });
    }
    for (const listener of [...listeners]) listener({ take: 'n0123456789abcdef', ended: true });
    expect(on.onStopped).toHaveBeenCalledTimes(1);
    expect(listeners).toHaveLength(0);
    expect(holdListeners).toHaveLength(0);

    // Numbers and the variant only: nothing else of what the App said is kept.
    const attempt = trace.toEvent('sent').attempts[0];
    expect(attempt).toMatchObject({
      kind: 'native', mic: 'ok', end: 'rendered',
      hold: 'early', holdRecMs: 200, holdLiveMs: 61, holdBufMs: 160, holdAdoptMs: 231, holdUpMs: 2_480, holdActMs: 38, holdStartMs: 21,
    });
    expect(Object.keys(attempt).filter((key) => key.startsWith('hold')).sort()).toEqual(
      ['hold', 'holdActMs', 'holdAdoptMs', 'holdBufMs', 'holdLiveMs', 'holdRecMs', 'holdStartMs', 'holdUpMs'],
    );
  });

  it('hands on what was recorded before the take was taken 100 ms at a time, in order', async () => {
    const on = handlers();
    const capture = new NativePcmCapture(on, 5_000, undefined, 'n0123456789abcdef');
    await capture.start();
    // 1.2 s since touch-down, in the one event the App sends first: live recognition takes
    // frames, not one of any size.
    const since = Array.from({ length: 19_200 }, (_, index) => index % 30_000);
    for (const listener of [...listeners]) listener({ take: 'n0123456789abcdef', pcm: pcm(since) });
    expect(on.onSamples).toHaveBeenCalledTimes(12);
    const handed = on.onSamples.mock.calls.flatMap(([samples]) => Array.from(samples as Int16Array));
    expect(on.onSamples.mock.calls.every(([samples]) => (samples as Int16Array).length === 1_600)).toBe(true);
    expect(handed).toEqual(since);
    capture.stop();
    for (const listener of [...listeners]) listener({ take: 'n0123456789abcdef', ended: true });
  });

  it('says so when the App no longer has the take it was to take', async () => {
    const trace = new VoiceCaptureTrace('hold');
    const on = handlers();
    adoptAnswer = async () => {
      throw { code: 'failed', message: 'gone' };
    };
    const capture = new NativePcmCapture(on, 5_000, trace, 'n0123456789abcdef');
    const failure = await capture.start().then(() => null, (error: unknown) => error);
    expect(isNativeHoldGone(failure)).toBe(true);
    expect(isNativeHoldGone(new Error('other'))).toBe(false);
    expect(on.onCaptureState).not.toHaveBeenCalled();
    expect(listeners).toHaveLength(0);
    expect(holdListeners).toHaveLength(0);
    expect(trace.toEvent('failed').attempts[0]).toMatchObject({ kind: 'native', end: 'error' });
  });

  it('records through the App only where it can and its microphone is allowed', async () => {
    expect(await nativeCaptureAllowed()).toBe(true);
    // The first answer to the microphone question is WebKit's.
    permission = 'prompt';
    expect(await nativeCaptureAllowed()).toBe(false);
    permission = 'denied';
    expect(await nativeCaptureAllowed()).toBe(false);
    permission = 'granted';
    install({ voice: 1 });
    expect(await nativeCaptureAllowed()).toBe(false);
    delete (window as { avibeNative?: unknown }).avibeNative;
    expect(await nativeCaptureAllowed()).toBe(false);

    // The instance's off switch: tap and Show Page takes are the page's again.
    install();
    try {
      noteAppVoice({ app_hold: false });
      expect(await nativeCaptureAllowed()).toBe(false);
    } finally {
      noteAppVoice({ app_hold: true });
    }
    expect(await nativeCaptureAllowed()).toBe(true);
  });

  it('reports a take the App ended on its own as a capture failure, keeping what came', async () => {
    const on = handlers();
    const capture = new NativePcmCapture(on, 5_000);
    await capture.start();
    emit({ pcm: pcm([1, 2, 3]) });
    emit({ ended: true, reason: 'interrupted' });
    expect(on.onSamples).toHaveBeenCalledTimes(1);
    expect(on.onError).toHaveBeenCalledWith(new Error('native voice capture ended: interrupted'));
    expect(on.onStopped).toHaveBeenCalledTimes(1);
    expect(on.onError.mock.invocationCallOrder[0]).toBeLessThan(on.onStopped.mock.invocationCallOrder[0]);
  });

  it('records why the App ended a take, apart from a release', async () => {
    const trace = new VoiceCaptureTrace('hold');
    const capture = new NativePcmCapture(handlers(), 5_000, trace);
    await capture.start();
    emit({ pcm: pcm([1, 2, 3]) });
    emit({ ended: true, reason: 'route' });
    expect(trace.toEvent('interrupted').attempts[0]).toMatchObject({ kind: 'native', end: 'error', nativeEnd: 'route' });

    const other = new VoiceCaptureTrace('hold');
    install();
    const second = new NativePcmCapture(handlers(), 5_000, other);
    await second.start();
    emit({ ended: true, reason: 'something new' });
    expect(other.toEvent('interrupted').attempts[0]).toMatchObject({ end: 'error', nativeEnd: 'other' });
  });

  it('paces the loudness of a 100 ms chunk out one reading per 32 ms, not in a burst', async () => {
    vi.useFakeTimers();
    const on = handlers();
    const capture = new NativePcmCapture(on, 5_000);
    await capture.start();
    emit({ pcm: pcm(new Array(1_600).fill(8_192)) });
    expect(on.onLevel).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(31);
    expect(on.onLevel).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    expect(on.onLevel).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(32);
    expect(on.onLevel).toHaveBeenCalledTimes(3);
    vi.advanceTimersByTime(320);
    expect(on.onLevel).toHaveBeenCalledTimes(3);
    // None is left to come after the take.
    emit({ pcm: pcm(new Array(1_600).fill(8_192)) });
    capture.stop();
    emit({ ended: true });
    const after = on.onLevel.mock.calls.length;
    vi.advanceTimersByTime(320);
    expect(on.onLevel).toHaveBeenCalledTimes(after);
  });

  it('ends on its own when the App never answers the stop', async () => {
    vi.useFakeTimers();
    const on = handlers();
    const capture = new NativePcmCapture(on, 5_000);
    await capture.start();
    capture.stop();
    vi.advanceTimersByTime(4_999);
    expect(on.onStopped).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(on.onStopped).toHaveBeenCalledTimes(1);
  });

  it('keeps its readings level with the audio when its timers fire late', async () => {
    vi.useFakeTimers();
    const on = handlers();
    const capture = new NativePcmCapture(on, 5_000);
    await capture.start();
    // Three seconds of audio in 100 ms chunks (93.75 readings), each timer firing 3 ms late:
    // paced 35 ms apart they would fall ~10 readings (300 ms) behind.
    const realSetTimeout = globalThis.setTimeout;
    const late = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((fn: () => void, ms?: number) => realSetTimeout(fn, (ms ?? 0) + 3)) as typeof setTimeout);
    for (let chunk = 0; chunk < 30; chunk += 1) {
      emit({ pcm: pcm(new Array(1_600).fill(8_192)) });
      vi.advanceTimersByTime(100);
    }
    late.mockRestore();
    // Paced against a deadline: what is still queued is at most about one chunk's worth.
    expect(on.onLevel.mock.calls.length).toBeGreaterThanOrEqual(90);
    capture.stop();
  });

  it('asks the App for nothing when its take was stopped before it began', async () => {
    const on = handlers();
    const capture = new NativePcmCapture(on, 5_000);
    capture.stop();
    await capture.start();
    expect(call).not.toHaveBeenCalled();
    expect(listeners).toHaveLength(0);
    expect(on.onStopped).toHaveBeenCalledTimes(1);
  });

  it("starts no App microphone for a take whose page was already hidden", async () => {
    const hidden = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    try {
      const pipeline = new VoiceRecordingPipeline({
        stream: { getTracks: () => [], getAudioTracks: () => [] } as unknown as MediaStream,
        segmentMs: 30_000,
        createCapture: nativeCaptureFactory,
        onSegment: () => undefined,
        onStopped: () => undefined,
      });
      expect(await pipeline.start()).toBe(false);
    } finally {
      hidden.mockRestore();
    }
    expect(call).not.toHaveBeenCalled();
    expect(listeners).toHaveLength(0);
  });

  it('names a refused start as the microphone errors the hold already explains', async () => {
    for (const [message, name] of [['permission', 'NotAllowedError'], ['busy', 'NotReadableError'], ['engine:-10868', 'other']]) {
      startAnswer = async () => {
        throw { code: 'failed', message };
      };
      const trace = new VoiceCaptureTrace('hold');
      const capture = new NativePcmCapture(handlers(), 5_000, trace);
      await expect(capture.start()).rejects.toMatchObject({ name });
      const event = trace.toEvent('failed');
      expect(event.attempts[0]).toMatchObject({ kind: 'native', mic: name, end: 'error' });
      // The take's own microphone answer is the App's: a refused start is no microphone.
      expect(event).toMatchObject({ mic: name, captureClass: 'no_microphone' });
      expect(event.micMs).toBeGreaterThanOrEqual(0);
      expect(listeners).toHaveLength(0);
    }
  });

  it('starts nothing more when the take was stopped while the App was starting', async () => {
    let answer!: (value: unknown) => void;
    startAnswer = () => new Promise((resolve) => {
      answer = resolve;
    });
    const on = handlers();
    const capture = new NativePcmCapture(on, 5_000);
    const started = capture.start();
    await Promise.resolve();
    capture.stop();
    answer({ sampleRate: 16_000 });
    await started;
    expect(on.onCaptureState).not.toHaveBeenCalled();
    expect(listeners).toHaveLength(1);
    expect(call.mock.calls.filter(([method]) => method === 'voice.captureStop')).toHaveLength(2);
    emit({ ended: true });
    expect(on.onStopped).toHaveBeenCalledTimes(1);
  });

  it("drives the take's pipeline: its segments are the App's audio", async () => {
    const segments: number[] = [];
    const stopped = vi.fn();
    const pipeline = new VoiceRecordingPipeline({
      stream: { getTracks: () => [], getAudioTracks: () => [] } as unknown as MediaStream,
      segmentMs: 30_000,
      createCapture: nativeCaptureFactory,
      onSegment: (_blob, metadata) => segments.push(metadata.durationMs),
      onStopped: stopped,
    });
    expect(await pipeline.start()).toBe(true);
    emit({ pcm: pcm(new Array(16_000).fill(1_000)) });
    pipeline.finish();
    emit({ ended: true });
    await vi.waitFor(() => expect(stopped).toHaveBeenCalled());
    expect(segments).toEqual([1_000]);
  });
});
