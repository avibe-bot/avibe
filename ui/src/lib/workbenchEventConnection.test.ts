import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  WorkbenchEventReconnectLoop,
  WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
  WORKBENCH_EVENT_OPEN_TIMEOUT_MS,
  declaredWorkbenchHeartbeatInterval,
  heartbeatCoversGap,
  isWorkbenchHeartbeatFresh,
  parseWorkbenchHeartbeatInterval,
  workbenchEventStaleAfterMs,
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

describe('workbench event stream liveness', () => {
  const now = 1_700_000_000_000;

  it('only vouches for a stream inside its own staleness window', () => {
    const interval = WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS;
    const staleAfter = workbenchEventStaleAfterMs(interval);

    // A server too old to send heartbeats never stamps one, so its streams
    // never speak for a gap -- the pre-heartbeat behavior, unchanged.
    expect(isWorkbenchHeartbeatFresh(null, interval, now)).toBe(false);
    expect(isWorkbenchHeartbeatFresh(now, interval, now)).toBe(true);
    // A few missed beats are jitter, not an outage.
    expect(isWorkbenchHeartbeatFresh(now - interval, interval, now)).toBe(true);
    expect(isWorkbenchHeartbeatFresh(now - (staleAfter - 1), interval, now)).toBe(true);
    expect(isWorkbenchHeartbeatFresh(now - staleAfter, interval, now)).toBe(false);
    // A clock that moved backwards cannot date the stamp; unproven is the safe
    // reading, because it costs one redundant read rather than a missed one.
    expect(isWorkbenchHeartbeatFresh(now + 1, interval, now)).toBe(false);
  });

  it('sizes the window from the server cadence but bounds what it will believe', () => {
    // The point of shipping the cadence: a slower server widens the tolerance
    // instead of the client hardcoding a guess about the interval.
    expect(workbenchEventStaleAfterMs(60_000)).toBeGreaterThan(
      workbenchEventStaleAfterMs(WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS),
    );
    // ...and a floor keeps a fast cadence from making jitter look like death.
    expect(workbenchEventStaleAfterMs(1_000)).toBe(
      workbenchEventStaleAfterMs(WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS),
    );

    expect(parseWorkbenchHeartbeatInterval(20_000)).toBe(20_000);
    for (const unusable of [undefined, null, 'soon', NaN, Infinity, {}]) {
      expect(parseWorkbenchHeartbeatInterval(unusable)).toBe(
        WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
      );
    }
    // A window no real gap could exceed would switch the check off, so a
    // declared cadence is clamped rather than trusted.
    const clamped = parseWorkbenchHeartbeatInterval(Number.MAX_SAFE_INTEGER);
    expect(isWorkbenchHeartbeatFresh(now - 3_600_000, clamped, now)).toBe(false);
    expect(parseWorkbenchHeartbeatInterval(-5)).toBeGreaterThan(0);
  });

  it('separates a cadence the server declared from one the client assumed', () => {
    // The two readers differ only in what they do with an unusable field, and
    // that difference is the whole point: a heartbeat is proof of life whatever
    // it says about cadence, while a handshake that declares nothing must not be
    // held to a deadline it never agreed to.
    for (const undeclared of [undefined, null, 'soon', NaN, Infinity, {}]) {
      expect(declaredWorkbenchHeartbeatInterval(undeclared)).toBeUndefined();
      expect(parseWorkbenchHeartbeatInterval(undeclared)).toBe(
        WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
      );
    }
    // A declaration is honored, and bounded by the same clamp, so a nonsense
    // promise cannot buy a window no real outage could exceed.
    expect(declaredWorkbenchHeartbeatInterval(20_000)).toBe(20_000);
    expect(declaredWorkbenchHeartbeatInterval(Number.MAX_SAFE_INTEGER)).toBe(
      parseWorkbenchHeartbeatInterval(Number.MAX_SAFE_INTEGER),
    );
    expect(declaredWorkbenchHeartbeatInterval(-5)).toBeGreaterThan(0);
  });

  const interval = WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS;
  const staleAfter = workbenchEventStaleAfterMs(interval);

  it('covers a gap only when a heartbeat lands inside it and still holds at its end', () => {
    // Stated as containment rather than as a list of scenarios. The gap is
    // [awaySince, now]; one heartbeat vouches for [beat, beat + staleAfter).
    // Coverage is that window reaching the gap's end while the beat itself falls
    // after the gap's start -- both ends, spelled independently of how the
    // implementation spells them, so dropping either one fails here.
    const covered = (awaySince: number, beat: number) =>
      awaySince < beat && beat <= now && now < beat + staleAfter;

    const offsets = [0, 1, 500, interval, staleAfter - 1, staleAfter, staleAfter + 1, staleAfter * 3];
    // A beat dated in the future is included: an unusable clock is not evidence.
    for (const awayAgo of offsets) {
      for (const beatAgo of [...offsets, -1]) {
        const awaySince = now - awayAgo;
        const lastHeartbeatAt = now - beatAgo;
        expect(
          heartbeatCoversGap({ lastHeartbeatAt, awaySince, intervalMs: interval, now }),
          `away ${awayAgo}ms ago, heartbeat ${beatAgo}ms ago`,
        ).toBe(covered(awaySince, lastHeartbeatAt));
      }
    }
  });

  it('reads an interval missing an end as uncovered', () => {
    // Both nulls say the same thing -- one end of the interval is missing, and a
    // comparison against a missing end has no answer. Unproven is the safe
    // reading: it costs a redundant read instead of silently stale data.
    for (const awaySince of [null, now - 1_000]) {
      for (const lastHeartbeatAt of [null, now]) {
        if (awaySince !== null && lastHeartbeatAt !== null) continue;
        expect(heartbeatCoversGap({ lastHeartbeatAt, awaySince, intervalMs: interval, now })).toBe(
          false,
        );
      }
    }
  });

  it('shrinks the untestable tail to one cadence without pretending to remove it', () => {
    // What this costs: a gap too short to contain a heartbeat is not covered, so
    // it pays the catch-up read that freshness alone would have skipped.
    expect(
      heartbeatCoversGap({
        lastHeartbeatAt: now - 1_000,
        awaySince: now - 500,
        intervalMs: interval,
        now,
      }),
    ).toBe(false);
    // What it cannot fix, recorded rather than patched: silence is not evidence,
    // so a stream that dies after its last heartbeat still reads as covering the
    // rest of a long gap. Only the watchdog closes that, by waiting for the
    // stream to speak again.
    expect(
      heartbeatCoversGap({
        lastHeartbeatAt: now - (staleAfter - 1),
        awaySince: now - staleAfter * 10,
        intervalMs: interval,
        now,
      }),
    ).toBe(true);
  });
});
