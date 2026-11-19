/** @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NativePcmCapture, nativeCaptureFactory } from './nativeVoiceCapture';
import { VoiceCaptureTrace } from './voiceCaptureTrace';
import { VoiceRecordingPipeline } from './voiceRecording';

// A take the iOS App records (ios/Avibe/NativeHoldCapture.swift), against a stand-in bridge:
// its PCM reaches the pipeline as the page's own capture's does, and its end is the take's.

type Detail = { take: string; pcm?: string; ended?: boolean; reason?: string };

let listeners: ((detail: Detail) => void)[] = [];
let call: ReturnType<typeof vi.fn>;
let startAnswer: () => Promise<unknown>;

const install = () => {
  listeners = [];
  call = vi.fn(async (method: string) => {
    if (method === 'voice.captureStart') return startAnswer();
    return null;
  });
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities: { voice: 1, voiceCapture: 1 },
      call,
      on: (event: string, handler: (detail: Detail) => void) => {
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
