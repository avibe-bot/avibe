import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  deleteMapValueIfCurrent,
  VoiceRecordingPipeline,
} from './voiceRecording';

class FakeRecorder {
  state: RecordingState = 'inactive';
  mimeType = 'audio/webm';
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor(
    readonly id: number,
    private readonly events: string[],
  ) {}

  start(): void {
    this.events.push(`start:${this.id}`);
    this.state = 'recording';
  }

  stop(): void {
    this.events.push(`stop:${this.id}`);
    this.state = 'inactive';
  }

  settle(data: string): void {
    this.ondataavailable?.({ data: new Blob([data]) });
    this.onstop?.();
  }
}

const setup = () => {
  const events: string[] = [];
  const recorders: FakeRecorder[] = [];
  const track = { stop: vi.fn() };
  const stream = { getTracks: () => [track] } as unknown as MediaStream;
  const onSegment = vi.fn<(blob: Blob) => void>();
  const onStopped = vi.fn();
  const pipeline = new VoiceRecordingPipeline({
    stream,
    audioBitsPerSecond: 32_000,
    segmentMs: 60_000,
    onSegment,
    onStopped,
    createRecorder: () => {
      const recorder = new FakeRecorder(recorders.length, events);
      recorders.push(recorder);
      return recorder;
    },
  });
  return { events, onSegment, onStopped, pipeline, recorders, track };
};

afterEach(() => {
  vi.useRealTimers();
});

describe('VoiceRecordingPipeline', () => {
  it('starts the next independently decodable segment before stopping the current one', () => {
    vi.useFakeTimers();
    const { events, pipeline, recorders } = setup();

    pipeline.start();
    vi.advanceTimersByTime(60_000);

    expect(recorders).toHaveLength(2);
    expect(events).toEqual(['start:0', 'start:1', 'stop:0']);
  });

  it('delivers overlapping recorder results in capture order before completing', async () => {
    vi.useFakeTimers();
    const { onSegment, onStopped, pipeline, recorders, track } = setup();

    pipeline.start();
    vi.advanceTimersByTime(60_000);
    pipeline.finish();

    recorders[1]!.settle('second');
    expect(onSegment).not.toHaveBeenCalled();
    expect(onStopped).not.toHaveBeenCalled();

    recorders[0]!.settle('first');
    expect(await Promise.all(onSegment.mock.calls.map(([blob]) => blob.text()))).toEqual([
      'first',
      'second',
    ]);
    expect(onStopped).toHaveBeenCalledWith('finish');
    expect(track.stop).toHaveBeenCalledTimes(1);
  });

  it('discards every unsettled segment when aborted', () => {
    vi.useFakeTimers();
    const { onSegment, onStopped, pipeline, recorders } = setup();

    pipeline.start();
    vi.advanceTimersByTime(60_000);
    pipeline.abort();
    recorders[0]!.settle('first');
    recorders[1]!.settle('second');

    expect(onSegment).not.toHaveBeenCalled();
    expect(onStopped).toHaveBeenCalledWith('abort');
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
