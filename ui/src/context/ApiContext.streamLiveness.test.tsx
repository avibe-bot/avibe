/* @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import {
  WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
  WORKBENCH_EVENT_RETRY_INITIAL_MS,
  workbenchEventStaleAfterMs,
} from '../lib/workbenchEventConnection';
import { ApiProvider, useApi } from './ApiContext';

const STALE_AFTER_MS = workbenchEventStaleAfterMs(WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS);

/**
 * Enough of EventSource to be inspected and fed frames. jsdom ships none, and
 * the behavior under test is precisely what the real one cannot express: a
 * socket that died while the page was suspended, still reporting `OPEN`.
 */
class FakeEventSource {
  static readonly OPEN = 1;
  static instances: FakeEventSource[] = [];

  readyState = FakeEventSource.OPEN;
  closed = false;
  onerror: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<string, Set<(event: MessageEvent) => void>>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    const bucket = this.listeners.get(type) ?? new Set();
    bucket.add(listener);
    this.listeners.set(type, bucket);
  }

  removeEventListener(type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  emit(type: string, data: unknown): void {
    const event = { data: JSON.stringify(data) } as MessageEvent;
    for (const listener of [...(this.listeners.get(type) ?? [])]) listener(event);
  }

  static latest(): FakeEventSource {
    const source = FakeEventSource.instances.at(-1);
    if (!source) throw new Error('no EventSource was opened');
    return source;
  }
}

/**
 * The one signal consumers catch up on. Every gap has to arrive here, and a
 * stream that never broke must not produce it a second time.
 */
const onConnected = vi.fn();

const Subscriber = () => {
  const api = useApi();
  useEffect(() => api.connectWorkbenchEvents({ onConnected }), [api]);
  return null;
};

/** A heartbeat, which is also the server declaring the cadence it owes. */
const emitHeartbeat = () => {
  FakeEventSource.latest().emit('heartbeat', {
    interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
  });
};

/**
 * What the UI server reports about its own leg to the controller. It sends one
 * of these immediately after the handshake on every stream it opens, which is
 * why every mount below sends one too: a fixture that skipped it would be
 * testing a client talking to a server that does not exist.
 */
const emitControllerLeg = (connected: boolean) => {
  FakeEventSource.latest().emit('workbench.events.bridge.status', {
    type: 'workbench.events.bridge.status',
    data: { connected },
  });
};

/** A handshake from a server that declares the cadence it owes. */
const emitHandshake = (subId: number) => {
  FakeEventSource.latest().emit('connected', {
    sub_id: subId,
    interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
  });
};

/** A handshake from a server too old to declare one: nothing to enforce. */
const emitUndeclaredHandshake = (subId: number) => {
  FakeEventSource.latest().emit('connected', { sub_id: subId });
};

/** Open a stream and complete its handshake, as a real connect would. */
const mountStream = () => {
  render(<ApiProvider><Subscriber /></ApiProvider>);
  emitHandshake(1);
  emitControllerLeg(true);
};

/** The same, against a server that never declares a cadence. */
const mountUndeclaredStream = () => {
  render(<ApiProvider><Subscriber /></ApiProvider>);
  emitUndeclaredHandshake(1);
  emitControllerLeg(true);
};

/**
 * The ordinary case: a stream that handshook and then proved itself. A handshake
 * is a promise about the future, not evidence about the past, so anything that
 * expects a stream to vouch for a gap has to start here rather than at the
 * handshake.
 */
const mountConnectedStream = () => {
  mountStream();
  emitHeartbeat();
};

const setVisibility = (state: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state });
  document.dispatchEvent(new Event('visibilitychange'));
};

/**
 * The page leaving and coming back: the gap the gate is asked to account for.
 * `beat` is whether the stream spoke while the page was away, which is the only
 * evidence that can cover an interval -- a heartbeat from before it was away
 * says nothing about what happened during it.
 */
const awayAndBack = ({ beat = false, awayMs = 1_000 } = {}) => {
  setVisibility('hidden');
  vi.advanceTimersByTime(awayMs);
  if (beat) emitHeartbeat();
  setVisibility('visible');
};

/** Regaining the network: a gap nothing here watched open, so an undated one. */
const networkBack = () => {
  window.dispatchEvent(new Event('online'));
};

beforeEach(() => {
  vi.useFakeTimers();
  // jsdom reports no window focus, and a page that never counts as active never
  // counts as coming back either. These tests are about what happens when it
  // does, so give it the focus a real browser tab has.
  document.hasFocus = () => true;
  apiFetch.mockReset();
  apiFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  showToast.mockReset();
  onConnected.mockReset();
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(document, 'hasFocus');
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('ApiProvider workbench stream liveness', () => {
  it('leaves a stream that keeps proving itself alone across reactivations', () => {
    mountConnectedStream();
    expect(onConnected).toHaveBeenCalledTimes(1);

    // The whole optimization: returning to a page whose stream kept running
    // through the gap must not recycle it, and so must not make consumers
    // re-read what it has already delivered.
    awayAndBack({ beat: true });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(onConnected).toHaveBeenCalledTimes(1);

    // Heartbeats keep proving it well past the stale window, which also shows
    // the watchdog re-arming on each one: without that, the deadline armed at
    // the handshake would have killed this stream during the second pass.
    for (let pass = 0; pass < 3; pass += 1) {
      vi.advanceTimersByTime(STALE_AFTER_MS - 1_000);
      emitHeartbeat();
      awayAndBack({ beat: true });
    }
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.latest().closed).toBe(false);
    expect(onConnected).toHaveBeenCalledTimes(1);
  });

  it('recycles a stream that stopped proving itself while nothing else noticed', () => {
    mountConnectedStream();
    const zombie = FakeEventSource.latest();
    expect(onConnected).toHaveBeenCalledTimes(1);

    // No heartbeat for longer than the stale window, with no page event and no
    // `error` -- a socket dropped by a proxy that the browser keeps reporting
    // `OPEN`. Nobody would ever ask about it, so the watchdog has to.
    vi.advanceTimersByTime(STALE_AFTER_MS);
    expect(zombie.closed).toBe(true);
    // Not a moment earlier than that: the catch-up is worth nothing until a
    // stream is actually carrying events again.
    expect(onConnected).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(WORKBENCH_EVENT_RETRY_INITIAL_MS);
    expect(FakeEventSource.instances).toHaveLength(2);
    emitHandshake(2);
    expect(onConnected).toHaveBeenCalledTimes(2);
  });

  it('defers to the reactivation edge while the page is hidden', () => {
    mountConnectedStream();
    const zombie = FakeEventSource.latest();

    // A hidden page cannot hold a stream open and its timers are throttled or
    // frozen, so the watchdog declines to judge -- reopening from there is the
    // burst of hidden-tab reconnects this whole design exists to avoid.
    setVisibility('hidden');
    vi.advanceTimersByTime(STALE_AFTER_MS * 2);
    expect(zombie.closed).toBe(false);
    expect(zombie.readyState).toBe(FakeEventSource.OPEN);
    expect(onConnected).toHaveBeenCalledTimes(1);

    // Coming back is where that stream is asked to account for the gap. It
    // cannot, so it is replaced -- and the edge itself carries the catch-up,
    // because the replacement may be several backoff windows away.
    setVisibility('visible');
    expect(zombie.closed).toBe(true);
    expect(onConnected).toHaveBeenCalledTimes(2);

    emitHandshake(2);
    expect(onConnected).toHaveBeenCalledTimes(3);
  });

  it('catches consumers up on the wake edge, not on the stream it is replacing', () => {
    mountConnectedStream();
    expect(onConnected).toHaveBeenCalledTimes(1);

    awayAndBack({ awayMs: STALE_AFTER_MS });

    // Recovery must not depend on the thing being recovered. The stale stream is
    // gone and its replacement has not handshaken -- and against a stopped
    // server or an offline network it never will -- so waiting for that
    // handshake would leave a returning page showing what it had before it was
    // hidden, with nothing on the way.
    expect(onConnected).toHaveBeenCalledTimes(2);
    // And it is the edge's own catch-up: the stale stream is closed and its
    // replacement has not reported in, so nothing else could have produced it.
    expect(FakeEventSource.instances).toHaveLength(2);

    // Repeated edges on a stream that still cannot prove itself each pay for
    // themselves, exactly as the unconditional refetch did before this change.
    awayAndBack();
    expect(onConnected).toHaveBeenCalledTimes(3);
  });

  it('does not let a heartbeat from before the gap speak for it', () => {
    mountConnectedStream();
    expect(onConnected).toHaveBeenCalledTimes(1);

    // A suspension shorter than the freshness window. The newest heartbeat is
    // still fresh by the clock, but it arrived before the page left: a frozen
    // tab receives nothing, so a window with no heartbeat in it is not the
    // scheduling jitter that tolerance exists for. It is silence.
    awayAndBack({ awayMs: 5_000 });
    expect(onConnected).toHaveBeenCalledTimes(2);

    // The socket itself is still plausible, so it is kept: recycling on every
    // short tab switch would cost more than the read it saves, and the watchdog
    // is already watching for the heartbeat this stream now owes.
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.latest().closed).toBe(false);
  });

  it('treats a network return as a gap nothing can vouch for', () => {
    mountConnectedStream();
    expect(onConnected).toHaveBeenCalledTimes(1);

    // Nothing here watched the connection drop, so no heartbeat can be placed
    // inside the gap and the catch-up is the only safe reading -- what the
    // unconditional refetch did before this change, on an event that fires when
    // the network actually changes rather than on every focus move.
    networkBack();
    expect(onConnected).toHaveBeenCalledTimes(2);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('does not let a replacement stream inherit the old one as proof of life', () => {
    mountConnectedStream();
    const first = FakeEventSource.latest();

    awayAndBack({ awayMs: STALE_AFTER_MS });
    expect(first.closed).toBe(true);

    // The stale stream was recycled; until the replacement proves itself there
    // is nothing vouching for it, so a second return is another gap rather than
    // a stream that accounted for one.
    const replacement = FakeEventSource.latest();
    expect(replacement).not.toBe(first);
    awayAndBack();
    expect(replacement.closed).toBe(true);

    // Once a stream proves itself across the gap, it speaks for itself again --
    // and the return is free, which is the entire point of the design. Both legs
    // report in, because a replacement stream inherits nothing: the old one's
    // controller-leg report went with it.
    emitHandshake(2);
    emitControllerLeg(true);
    emitHeartbeat();
    const proven = FakeEventSource.latest();
    const catchUps = onConnected.mock.calls.length;
    awayAndBack({ beat: true });
    expect(FakeEventSource.latest()).toBe(proven);
    expect(proven.closed).toBe(false);
    expect(onConnected).toHaveBeenCalledTimes(catchUps);
  });

  it('ends a controller-leg gap in place, without recycling the browser leg', () => {
    mountConnectedStream();

    // A report of the level it is already at is not a recovery, any more than
    // the opening one the mount already sent was. A stream that just dispatched
    // its handshake catch-up must not be charged a second one, or every connect
    // would cost double what it saves.
    emitControllerLeg(true);
    expect(onConnected).toHaveBeenCalledTimes(1);

    // While that leg is down the controller publishes into a severed bridge and
    // nothing replays it, so this is a real gap -- one the browser socket sails
    // straight through, heartbeating, with no error to report.
    emitControllerLeg(false);
    vi.advanceTimersByTime(STALE_AFTER_MS - 1_000);
    emitHeartbeat();
    expect(onConnected).toHaveBeenCalledTimes(1);

    emitControllerLeg(true);
    expect(onConnected).toHaveBeenCalledTimes(2);
    // Reopening this socket would have inherited the same severed bridge, so the
    // recovery is announced on the stream that stayed up rather than by making a
    // new one.
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.latest().closed).toBe(false);

    // And it is the transition that carries the meaning, every time it happens.
    emitControllerLeg(false);
    emitControllerLeg(true);
    expect(onConnected).toHaveBeenCalledTimes(3);
  });

  it('does not let a live socket vouch for a gap the controller leg was down for', () => {
    mountConnectedStream();
    emitControllerLeg(false);
    const socket = FakeEventSource.latest();
    const catchUps = onConnected.mock.calls.length;

    // The two rulings meet here, and each keeps its own scope. The socket
    // heartbeats through the whole gap, so the transport has nothing to repair
    // and is left alone -- reopening it would inherit the same severed bridge.
    // But what it carried across that gap came from a bridge that was down, so
    // the interval is not accounted for and the returning page reads.
    awayAndBack({ beat: true });
    expect(onConnected).toHaveBeenCalledTimes(catchUps + 1);
    expect(FakeEventSource.latest()).toBe(socket);
    expect(socket.closed).toBe(false);

    // Every return during the outage pays, exactly as an unconditional refetch
    // would: nothing here can shorten a gap that is still open.
    awayAndBack({ beat: true });
    expect(onConnected).toHaveBeenCalledTimes(catchUps + 2);

    // And once both legs are carrying again, returns are free again -- the
    // recovery itself is the last catch-up the outage costs.
    emitControllerLeg(true);
    const settled = onConnected.mock.calls.length;
    awayAndBack({ beat: true });
    expect(onConnected).toHaveBeenCalledTimes(settled);
  });

  it('never holds a server to a cadence it did not declare', () => {
    mountUndeclaredStream();
    const legacy = FakeEventSource.latest();

    // An older server -- a rollback under a tab that stayed open -- completes
    // the handshake and then says nothing more. A deadline is a claim about a
    // cadence, and this server made none, so there is nothing to enforce:
    // closing it on a timer would recycle a healthy stream every stale window,
    // forever, charging every consumer a catch-up each round.
    vi.advanceTimersByTime(STALE_AFTER_MS * 4);
    expect(legacy.closed).toBe(false);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(onConnected).toHaveBeenCalledTimes(1);

    // But it never counts as proven either, not even for one cadence: the
    // handshake says this stream is alive now, which is not the question. So the
    // reactivation edge recycles it immediately and pays for the catch-up -- the
    // pre-heartbeat behavior this optimization replaces, and no worse than it.
    awayAndBack();
    expect(legacy.closed).toBe(true);
    expect(onConnected).toHaveBeenCalledTimes(2);
  });

  it('holds a stream to the cadence its handshake promised, heartbeat or not', () => {
    mountStream();
    const silent = FakeEventSource.latest();

    // The hole a stamp-only clock cannot express: this stream opened, promised a
    // cadence, and then went quiet without ever sending a first heartbeat. There
    // is nothing to stamp, so the promise itself has to start the clock -- or a
    // stream that dies this way is watched by nobody, and the browser will keep
    // reporting the dead socket `OPEN`.
    vi.advanceTimersByTime(STALE_AFTER_MS);
    expect(silent.closed).toBe(true);

    // A heartbeat then restarts that same clock rather than replacing the
    // mechanism, so a stream that keeps proving itself is never recycled.
    vi.advanceTimersByTime(WORKBENCH_EVENT_RETRY_INITIAL_MS);
    emitHandshake(2);
    const proven = FakeEventSource.latest();
    for (let pass = 0; pass < 3; pass += 1) {
      vi.advanceTimersByTime(STALE_AFTER_MS - 1_000);
      emitHeartbeat();
    }
    expect(proven.closed).toBe(false);
  });

  it('keeps a declared cadence when a later handshake carries none', () => {
    mountStream();
    const declared = FakeEventSource.latest();

    // Only a frame carrying a cadence may speak for one. A `connected` without
    // one is either an older UI server's handshake or some future producer of
    // the same event name; arriving late in the window, one allowed to speak
    // would either retire the deadline this server promised or push it out by a
    // full window -- and this stream is already dead, having never sent a
    // heartbeat. (The controller relay that first motivated this rule is gone:
    // its frame is consumed by the bridge, which publishes the leg's status
    // instead.)
    vi.advanceTimersByTime(STALE_AFTER_MS - 1_000);
    FakeEventSource.latest().emit('connected', { type: 'connected' });
    vi.advanceTimersByTime(1_000);
    expect(declared.closed).toBe(true);
  });
});
