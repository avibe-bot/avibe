import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  WorkbenchEventReconnectLoop,
  WORKBENCH_EVENT_OPEN_TIMEOUT_MS,
} from './workbenchEventConnection';

describe('WorkbenchEventReconnectLoop', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('retries terminal failures forever with bounded backoff and resets after recovery', () => {
    vi.useFakeTimers();
    const reconnect = vi.fn();
    const loop = new WorkbenchEventReconnectLoop({
      reconnect,
      isVisible: () => true,
      isStreamLive: () => false,
    });

    for (const delayMs of [1_000, 2_000, 4_000, 8_000, 15_000, 15_000]) {
      loop.failed();
      vi.advanceTimersByTime(delayMs - 1);
      expect(reconnect).toHaveBeenCalledTimes(0);
      vi.advanceTimersByTime(1);
      expect(reconnect).toHaveBeenCalledTimes(1);
      reconnect.mockClear();
    }

    loop.streamOpened();
    loop.failed();
    vi.advanceTimersByTime(999);
    expect(reconnect).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(reconnect).toHaveBeenCalledTimes(1);
  });

  it('replaces a hung connection attempt and wakes immediately on foreground signals', () => {
    vi.useFakeTimers();
    let visible = true;
    const reconnect = vi.fn();
    const loop = new WorkbenchEventReconnectLoop({
      reconnect,
      isVisible: () => visible,
      isStreamLive: () => false,
    });

    loop.attemptStarted();
    vi.advanceTimersByTime(WORKBENCH_EVENT_OPEN_TIMEOUT_MS - 1);
    expect(reconnect).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(reconnect).toHaveBeenCalledTimes(1);

    visible = false;
    loop.failed();
    vi.advanceTimersByTime(60_000);
    expect(reconnect).toHaveBeenCalledTimes(1);

    visible = true;
    loop.wake();
    expect(reconnect).toHaveBeenCalledTimes(2);

    loop.stop();
    loop.failed();
    loop.wake();
    vi.advanceTimersByTime(60_000);
    expect(reconnect).toHaveBeenCalledTimes(2);
  });

  it('leaves a live stream alone on wake while still dropping the backoff', () => {
    vi.useFakeTimers();
    let streamLive = false;
    const reconnect = vi.fn();
    const loop = new WorkbenchEventReconnectLoop({
      reconnect,
      isVisible: () => true,
      isStreamLive: () => streamLive,
    });

    for (const delayMs of [1_000, 2_000, 4_000, 8_000, 15_000]) {
      loop.failed();
      vi.advanceTimersByTime(delayMs);
    }
    expect(reconnect).toHaveBeenCalledTimes(5);
    reconnect.mockClear();

    // A retry is waiting out the ceiling when the stream comes back up.
    loop.failed();
    streamLive = true;
    loop.wake();
    vi.advanceTimersByTime(60_000);
    // A stream that never dropped missed nothing, so waking must not recycle
    // it -- every consumer refetches on reconnect to close a gap that a live
    // stream does not have. The queued retry is cancelled, not deferred.
    expect(reconnect).not.toHaveBeenCalled();

    // Clearing the backoff is the other half of waking: the next real drop
    // retries from the shortest delay instead of resuming at the ceiling.
    streamLive = false;
    loop.failed();
    vi.advanceTimersByTime(999);
    expect(reconnect).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(reconnect).toHaveBeenCalledTimes(1);
  });
});
