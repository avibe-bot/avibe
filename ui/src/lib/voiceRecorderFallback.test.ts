import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { decodeVoiceRecording, VOICE_RECORDER_SILENT_MS, VoiceRecorderSegments } from './voiceRecorderFallback';

// A take recorded without Web Audio (lib/voiceRecorderFallback): segments recorded one recorder
// each, handed over decoded and in order, or as they are where they cannot be decoded.

const recorders: FakeRecorder[] = [];
class FakeRecorder {
  static isTypeSupported = (type: string) => type === 'audio/mp4';
  state: 'inactive' | 'recording' = 'inactive';
  readonly mimeType = 'audio/mp4';
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  constructor() {
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
  data(bytes: number): void {
    this.ondataavailable?.({ data: new Blob([new Uint8Array(bytes)], { type: this.mimeType }) });
  }
}

/** Held while set: a decode that has not finished. */
let decoding: Promise<void> | null = null;
/** Decodes a recording to as many frames as it has bytes, at the context's rate; fails on 13. */
class FakeOfflineAudioContext {
  constructor(readonly channels: number, readonly length: number, readonly sampleRate: number) {}
  async decodeAudioData(data: ArrayBuffer) {
    if (data.byteLength === 13) throw new DOMException('Unable to decode audio data', 'EncodingError');
    if (decoding) await decoding;
    const length = data.byteLength;
    return {
      numberOfChannels: 2,
      length,
      getChannelData: (channel: number) => new Float32Array(length).fill(channel === 0 ? 0.5 : 0.25),
    };
  }
}

const handlers = () => ({
  onSamples: vi.fn(),
  onFile: vi.fn(),
  onDelivering: vi.fn(),
  onError: vi.fn(),
});

beforeEach(() => {
  vi.useFakeTimers();
  recorders.length = 0;
  decoding = null;
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  vi.stubGlobal('OfflineAudioContext', FakeOfflineAudioContext);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('decodeVoiceRecording', () => {
  it("gives the recording as mono PCM at the context's rate", async () => {
    const samples = await decodeVoiceRecording(new Blob([new Uint8Array(4)]), 16_000);
    // Two channels, 0.5 and 0.25: their mean.
    expect(Array.from(samples)).toEqual([12_288, 12_288, 12_288, 12_288]);
  });
});

describe('VoiceRecorderSegments', () => {
  const stream = {} as MediaStream;

  it('records a segment per recorder and hands them over decoded, in order', async () => {
    const to = handlers();
    const segments = new VoiceRecorderSegments(stream, 1_000, 16_000, to);
    segments.start();
    expect(segments.type).toBe('audio/mp4');
    recorders[0].data(3);
    await vi.advanceTimersByTimeAsync(1_000); // the next segment's recorder starts
    expect(recorders).toHaveLength(2);
    recorders[1].data(2);
    await segments.stop();

    expect(to.onSamples.mock.calls.map(([samples]) => (samples as Int16Array).length)).toEqual([3, 2]);
    expect(to.onFile).not.toHaveBeenCalled();
  });

  it('hands over a segment it cannot decode as it is, the last one marked so', async () => {
    const to = handlers();
    const segments = new VoiceRecorderSegments(stream, 60_000, 16_000, to);
    segments.start();
    recorders[0].data(13);
    await segments.stop();

    expect(to.onSamples).not.toHaveBeenCalled();
    expect(to.onFile).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ type: 'audio/mp4', size: 13 }), expect.any(Number), true);
  });

  it('says once when the microphone gives it nothing, however its segments turn over', async () => {
    const to = handlers();
    const segments = new VoiceRecorderSegments(stream, 1_000, 16_000, to);
    segments.start();
    await vi.advanceTimersByTimeAsync(VOICE_RECORDER_SILENT_MS);
    expect(to.onDelivering.mock.calls).toEqual([[false]]);
    recorders.at(-1)!.data(1);
    expect(to.onDelivering.mock.calls).toEqual([[false], [true]]);
    void segments.stop();
  });

  it('hands nothing over once discarded, though it was being decoded', async () => {
    let decoded!: () => void;
    decoding = new Promise((resolve) => {
      decoded = resolve;
    });
    const to = handlers();
    const segments = new VoiceRecorderSegments(stream, 60_000, 16_000, to);
    segments.start();
    recorders[0].data(3);
    void segments.stop();
    await vi.advanceTimersByTimeAsync(0); // its last segment is being decoded
    segments.discard();
    decoded();
    await vi.advanceTimersByTimeAsync(0);

    expect(to.onSamples).not.toHaveBeenCalled();
    expect(to.onFile).not.toHaveBeenCalled();
  });

  it('hands nothing over once discarded', async () => {
    const to = handlers();
    const segments = new VoiceRecorderSegments(stream, 60_000, 16_000, to);
    segments.start();
    recorders[0].data(3);
    segments.discard();
    await vi.advanceTimersByTimeAsync(0);

    expect(recorders[0].state).toBe('inactive');
    expect(to.onSamples).not.toHaveBeenCalled();
    expect(to.onFile).not.toHaveBeenCalled();
  });
});
