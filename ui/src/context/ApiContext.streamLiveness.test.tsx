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
  useEffect(() => api.connectWorkbenchEvents({ onConnected: () => onConnected() }), [api]);
  return null;
};

/** A heartbeat, which is also the server declaring the cadence it owes. */
const emitHeartbeat = () => {
  FakeEventSource.latest().emit('heartbeat', {
    interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
  });
};

/** What the UI server reports about its own leg to the controller. */
const emitControllerLeg = (connected: boolean) => {
  FakeEventSource.latest().emit('workbench.events.bridge.status', {
    type: 'workbench.events.bridge.status',
    data: { connected },
  });
};

/** The UI server admitting it discarded events for this subscriber. */
const emitDroppedEvents = (dropped: number) => {
  FakeEventSource.latest().emit('workbench.events.gap', { dropped });
};

/** Open a stream and complete its handshake, as a real connect would. */
const mountStream = () => {
  render(<ApiProvider><Subscriber /></ApiProvider>);
  FakeEventSource.latest().emit('connected', { sub_id: 1 });
};

/**
 * The ordinary case: a stream that handshook and then declared its cadence.
 * A handshake is not proof of continuity, so anything that expects a stream to
 * vouch for itself has to start here rather than at the handshake.
 */
const mountConnectedStream = () => {
  mountStream();
  emitHeartbeat();
};

const setVisibility = (state: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state });
  document.dispatchEvent(new Event('visibilitychange'));
};

/** The page or the network coming back, which is what the gate is asked about. */
const reactivate = () => {
  window.dispatchEvent(new Event('online'));
};

beforeEach(() => {
  vi.useFakeTimers();
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
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('ApiProvider workbench stream liveness', () => {
  it('leaves a stream that keeps proving itself alone across reactivations', () => {
    mountConnectedStream();
    expect(onConnected).toHaveBeenCalledTimes(1);

    // The whole optimization: returning to a page whose stream kept running
    // must not recycle it, and so must not make consumers re-read what it has
    // already delivered.
    reactivate();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(onConnected).toHaveBeenCalledTimes(1);

    // Heartbeats keep proving it well past the stale window, which also shows
    // the watchdog re-arming on each one: without that, the deadline armed at
    // the handshake would have killed this stream during the second pass.
    for (let pass = 0; pass < 3; pass += 1) {
      vi.advanceTimersByTime(STALE_AFTER_MS - 1_000);
      emitHeartbeat();
      reactivate();
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
    FakeEventSource.latest().emit('connected', { sub_id: 2 });
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
    // cannot, so it is replaced -- and the catch-up lands once, on the
    // replacement's handshake, rather than on the edge itself.
    setVisibility('visible');
    reactivate();
    expect(zombie.closed).toBe(true);
    expect(onConnected).toHaveBeenCalledTimes(1);

    FakeEventSource.latest().emit('connected', { sub_id: 2 });
    expect(onConnected).toHaveBeenCalledTimes(2);
  });

  it('does not let a replacement stream inherit the old one as proof of life', () => {
    mountConnectedStream();
    const first = FakeEventSource.latest();

    setVisibility('hidden');
    vi.advanceTimersByTime(STALE_AFTER_MS);
    setVisibility('visible');
    reactivate();
    expect(first.closed).toBe(true);

    // The stale stream was recycled; until the replacement completes its own
    // handshake there is nothing vouching for it, so a second return is another
    // gap rather than a stream that proved anything.
    const replacement = FakeEventSource.latest();
    expect(replacement).not.toBe(first);
    reactivate();
    expect(replacement.closed).toBe(true);
    expect(onConnected).toHaveBeenCalledTimes(1);

    // Once a stream does connect, it speaks for itself again.
    FakeEventSource.latest().emit('connected', { sub_id: 2 });
    expect(onConnected).toHaveBeenCalledTimes(2);
    reactivate();
    expect(FakeEventSource.latest().closed).toBe(false);
    expect(onConnected).toHaveBeenCalledTimes(2);
  });

  it('ends a controller-leg gap in place, without recycling the browser leg', () => {
    mountConnectedStream();

    // The leg's opening report is its state, not a recovery. A stream that just
    // dispatched its handshake catch-up must not be charged a second one, or
    // every connect would cost double what it saves.
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

  it('never lets a handshake alone vouch for a stream', () => {
    mountStream();
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
    // reactivation edge recycles it immediately -- the pre-heartbeat behavior
    // this optimization replaces, and no worse than it.
    reactivate();
    expect(legacy.closed).toBe(true);
  });

  it('puts a stream on a deadline as soon as its server declares one', () => {
    mountStream();
    const proven = FakeEventSource.latest();

    vi.advanceTimersByTime(1_000);
    emitHeartbeat();
    vi.advanceTimersByTime(STALE_AFTER_MS);
    expect(proven.closed).toBe(true);
  });

  it('treats discarded events as a gap on a stream that stayed healthy', () => {
    mountConnectedStream();
    expect(onConnected).toHaveBeenCalledTimes(1);

    // A full broker queue discards events and cannot replay them, so this
    // stream is alive but no longer complete. Nothing else can say so: the
    // socket never broke and the heartbeats keep proving it.
    emitDroppedEvents(3);
    expect(onConnected).toHaveBeenCalledTimes(2);

    // Announced in place, because the loss is behind the socket rather than in
    // it -- reopening would discard the same events again under the same load.
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.latest().closed).toBe(false);

    // And the stream still speaks for itself afterwards: a gap is news about
    // what was missed, not a verdict on whether the stream is alive.
    vi.advanceTimersByTime(STALE_AFTER_MS - 1_000);
    emitHeartbeat();
    reactivate();
    expect(FakeEventSource.latest().closed).toBe(false);
    expect(onConnected).toHaveBeenCalledTimes(2);
  });
});
