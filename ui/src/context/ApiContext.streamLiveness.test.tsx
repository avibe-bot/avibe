/* @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());

// The local message store only learns about the stream through these two calls;
// what it does with them is tested beside it (lib/localStore).
const localStore = vi.hoisted(() => ({
  beginLocalStoreRead: vi.fn(),
  forgetLocalStoreSession: vi.fn(async () => {}),
  applyLocalStoreSessionActivity: vi.fn(),
  noteLocalStoreStreamGap: vi.fn(),
  noteLocalStoreStreamState: vi.fn(),
  recordLocalStorePage: vi.fn(async () => {}),
  recordLocalStoreSession: vi.fn(async () => {}),
  recordLocalStoreStreamRow: vi.fn(async () => {}),
  wipeLocalStore: vi.fn(async () => {}),
}));

vi.mock('../lib/workbenchEventSource', () => ({
  // This suite exercises ApiContext's reconnect/catch-up owner. Transport
  // negotiation and real sockets are covered by the adapter/browser suites.
  createWorkbenchEventSource: (url: string) => new EventSource(url),
}));

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('../lib/localStore', () => localStore);
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import {
  WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
  WORKBENCH_EVENT_RETRY_INITIAL_MS,
  workbenchEventStaleAfterMs,
} from '../lib/workbenchEventConnection';
import { liveStreamVia, setLiveStreamVia } from '../lib/liveStreamVia';
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
const onMessageNew = vi.fn();
const onMessageUpdated = vi.fn();

const Subscriber = () => {
  const api = useApi();
  useEffect(() => api.connectWorkbenchEvents({ onConnected, onMessageNew, onMessageUpdated }), [api]);
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

/** A handshake whose stream says how it reached the instance. */
const emitHandshakeVia = (subId: number, via: 'direct' | 'other') => {
  FakeEventSource.latest().emit('connected', {
    sub_id: subId,
    interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
    via,
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

/**
 * A native shell with the `direct` capability. Its `direct.status` answers are
 * held until the test gives them; `emit` is the shell's event.
 */
const installDirectShell = (
  capabilities: Record<string, number> = { direct: 1 },
  platform: 'ios' | 'desktop' = 'ios',
) => {
  let deliver: (payload: unknown) => void = () => {};
  const answers: Array<(value: unknown) => void> = [];
  const call = vi.fn(
    (method: string) =>
      new Promise((resolve) => {
        if (method === 'direct.status') answers.push(resolve);
        else if (method === 'direct.handover') resolve({ closed: 1, kept: 0 });
        else resolve(null);
      }),
  );
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform,
      shellVersion: '1.0 (1)',
      capabilities,
      call,
      on: (event: string, handler: (payload: unknown) => void) => {
        if (event === 'direct.transportChanged') deliver = handler;
        return () => undefined;
      },
    },
  });
  return {
    call,
    statusCalls: () => answers.length,
    answer: async (
      index: number,
      closure: number,
      sequence = closure,
      preferred: 'tunnel' | 'direct' = 'tunnel',
    ) => {
      answers[index]({ preferred, sequence, closure });
      await vi.advanceTimersByTimeAsync(0);
    },
    handovers: () => call.mock.calls.filter(([method]) => method === 'direct.handover').length,
    malformed: async (index: number) => {
      answers[index]({ preferred: 'direct' });
      await vi.advanceTimersByTimeAsync(0);
    },
    emit: (payload: {
      preferred: 'tunnel' | 'direct';
      sequence: number;
      closure: number;
      path?: 'relay' | 'ipv4' | 'ipv6';
    }) => deliver(payload),
  };
};

/**
 * The direct state belongs to the document, so each of these cases mounts a
 * fresh copy of the provider and of the bridge module, as a new page would.
 */
const mountDirectPage = async () => {
  vi.resetModules();
  const fresh = await import('./ApiContext');
  const FreshSubscriber = () => {
    const api = fresh.useApi();
    useEffect(
      () => api.connectWorkbenchEvents({ onConnected, onMessageNew, onMessageUpdated }),
      [api],
    );
    return null;
  };
  render(
    <fresh.ApiProvider>
      <FreshSubscriber />
    </fresh.ApiProvider>,
  );
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
  localStore.noteLocalStoreStreamGap.mockReset();
  localStore.noteLocalStoreStreamState.mockReset();
  onMessageNew.mockReset();
  onMessageUpdated.mockReset();
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
});

// A hand-over shell wraps `fetch` to count the page's requests; each case
// starts from the original.
const originalFetch = window.fetch;

afterEach(() => {
  cleanup();
  window.fetch = originalFetch;
  Reflect.deleteProperty(window, 'avibeNative');
  Reflect.deleteProperty(document, 'hasFocus');
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('ApiProvider workbench stream liveness', () => {
  it('routes an action update without replaying a terminal message', () => {
    mountConnectedStream();
    const notice = { id: 'failed-notice', session_id: 'session', type: 'notify' };
    FakeEventSource.latest().emit('message.updated', { type: 'message.updated', data: notice });
    expect(onMessageUpdated).toHaveBeenCalledExactlyOnceWith(notice);
    expect(onMessageNew).not.toHaveBeenCalled();
  });

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

  it('says how the live stream came in only while it lives, and asks each new stream again', () => {
    setLiveStreamVia(null);
    render(<ApiProvider><Subscriber /></ApiProvider>);
    // Unknown, not "not direct", before any stream has answered for itself.
    expect(liveStreamVia()).toBeNull();
    const handshakeVia = (subId: number, via?: string) => FakeEventSource.latest().emit('connected', {
      sub_id: subId,
      interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
      ...(via ? { via } : {}),
    });
    handshakeVia(1, 'direct');
    emitControllerLeg(true);
    expect(liveStreamVia()).toBe('direct');

    const first = FakeEventSource.latest();
    awayAndBack({ awayMs: STALE_AFTER_MS });
    expect(first.closed).toBe(true);
    // Reconnecting: unknown again until the replacement answers.
    expect(liveStreamVia()).toBeNull();

    // The replacement came in another way: the main listener is only "other".
    handshakeVia(2, 'other');
    expect(liveStreamVia()).toBe('other');
    // A later replacement answers for itself, direct again.
    const second = FakeEventSource.latest();
    awayAndBack({ awayMs: STALE_AFTER_MS });
    expect(second.closed).toBe(true);
    handshakeVia(3, 'direct');
    expect(liveStreamVia()).toBe('direct');
    // A server too old to say is unknown, never "not direct".
    handshakeVia(4);
    expect(liveStreamVia()).toBeNull();
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

  it('tells the local message store about every gap consumers catch up on, and every loss of the stream', () => {
    // A tail the store recorded may claim nothing newer exists only while the
    // stream that would have delivered anything newer stayed continuous
    // (docs/plans/local-sync-store.md section 4.2).
    mountConnectedStream();
    expect(localStore.noteLocalStoreStreamState).toHaveBeenLastCalledWith(true);
    awayAndBack({ awayMs: 5_000 });
    networkBack();
    emitControllerLeg(false);
    expect(localStore.noteLocalStoreStreamState).toHaveBeenLastCalledWith(false);
    emitControllerLeg(true);

    expect(onConnected).toHaveBeenCalledTimes(4);
    expect(localStore.noteLocalStoreStreamGap).toHaveBeenCalledTimes(4);
    expect(localStore.noteLocalStoreStreamState).toHaveBeenLastCalledWith(true);
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

  it('opens the first stream only after the direct baseline, and at once without the capability', async () => {
    const shell = installDirectShell();
    await mountDirectPage();
    expect(shell.statusCalls()).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(0);

    await shell.answer(0, 4);
    expect(FakeEventSource.instances).toHaveLength(1);
    cleanup();
    FakeEventSource.instances = [];

    const plain = installDirectShell({ app: 1 });
    await mountDirectPage();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(plain.call).not.toHaveBeenCalled();
  });

  it('opens no stream while the baseline is unavailable, and opens once when the held answer comes', async () => {
    const shell = installDirectShell();
    const states: string[] = [];
    vi.resetModules();
    const fresh = await import('./ApiContext');
    const Watcher = () => {
      const api = fresh.useApi();
      useEffect(
        () => api.connectWorkbenchEvents({ onConnected, onConnectionState: (state) => states.push(state) }),
        [api],
      );
      return null;
    };
    render(
      <fresh.ApiProvider>
        <Watcher />
      </fresh.ApiProvider>,
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(states.at(-1)).toBe('reconnecting');

    // The shell holds its answer past the wait: unavailable, still no stream,
    // and the reconnect loop's own attempts open nothing either.
    await vi.advanceTimersByTimeAsync(30_000);
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(shell.statusCalls()).toBe(1);
    expect(states.at(-1)).toBe('reconnecting');

    await shell.answer(0, 2);
    expect(FakeEventSource.instances).toHaveLength(1);
    await shell.answer(0, 2);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('opens nothing after a malformed baseline, until a visible return reads a valid one', async () => {
    const shell = installDirectShell();
    await mountDirectPage();
    await shell.malformed(0);
    expect(FakeEventSource.instances).toHaveLength(0);

    setVisibility('hidden');
    setVisibility('visible');
    expect(shell.statusCalls()).toBe(2);
    await shell.answer(1, 0);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  // The hand-over from the public path to direct (docs/plans/remote-direct-ios.md section 7).

  const handoverShell = async () => {
    const shell = installDirectShell({ direct: 2 });
    await mountDirectPage();
    await shell.answer(0, 0);
    emitHandshake(1);
    emitControllerLeg(true);
    return shell;
  };

  it('hands the stream over once when the direct path becomes usable, and only again after it went back', async () => {
    const shell = await handoverShell();
    const first = FakeEventSource.latest();

    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    // Closed before the shell closes the quiet public connections, opened
    // again after: the new stream needs a new connection.
    expect(shell.handovers()).toBe(1);
    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
    emitHandshake(2);
    emitControllerLeg(true);

    // Still direct: nothing more.
    shell.emit({ preferred: 'direct', sequence: 2, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);

    // Back to the public path and direct again at once: re-armed, but not
    // sooner than the short spacing allows, so flips cause no storm.
    for (let sequence = 3; sequence < 13; sequence += 2) {
      shell.emit({ preferred: 'tunnel', sequence, closure: 0 });
      shell.emit({ preferred: 'direct', sequence: sequence + 1, closure: 0 });
      await vi.advanceTimersByTimeAsync(0);
    }
    await vi.advanceTimersByTimeAsync(4_000);
    expect(shell.handovers()).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(2);

    // Direct again once the short spacing has passed: followed at once.
    await vi.advanceTimersByTimeAsync(1_000);
    shell.emit({ preferred: 'tunnel', sequence: 13, closure: 0 });
    shell.emit({ preferred: 'direct', sequence: 14, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(2);
  });

  it('waits while the page uses the network, however long, and hands over when its last request ends', async () => {
    const shell = await handoverShell();
    const first = FakeEventSource.latest();
    const { beginPageRequest } = await import('../lib/pageConnections');
    const done = beginPageRequest();

    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(10_000);
    // A request still waiting for its answer: nothing is closed, and the
    // hand-over is not given up.
    expect(shell.handovers()).toBe(0);
    expect(first.closed).toBe(false);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(shell.handovers()).toBe(0);
    expect(first.closed).toBe(false);

    // The request ends: handed over the next time the page is looked at
    // (once a second by now), with no event of the shell, no resume and no
    // spacing in between.
    done();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(shell.handovers()).toBe(1);
    expect(first.closed).toBe(true);
  });

  it('gives up a hand-over it was waiting for once the shell prefers the public path', async () => {
    const shell = await handoverShell();
    const first = FakeEventSource.latest();
    const { beginPageRequest } = await import('../lib/pageConnections');
    const done = beginPageRequest();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(2_000);
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 0 });
    done();
    await vi.advanceTimersByTimeAsync(5_000);
    // No longer wanted when the page went quiet: nothing was closed.
    expect(shell.handovers()).toBe(0);
    expect(first.closed).toBe(false);

    // Direct again: the arm was not spent by waiting.
    shell.emit({ preferred: 'direct', sequence: 3, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
  });

  it('goes public, direct, lost, public, then direct again', async () => {
    const shell = await handoverShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    emitHandshake(2);
    emitControllerLeg(true);
    const onDirect = FakeEventSource.latest();

    // Lost: the closure rebuilds the stream once, onto the public path.
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(onDirect.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(3);
    expect(shell.handovers()).toBe(1);
    emitHandshake(3);
    emitControllerLeg(true);

    // Usable again later: one more hand-over.
    await vi.advanceTimersByTimeAsync(30_000);
    shell.emit({ preferred: 'direct', sequence: 3, closure: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(2);
    expect(FakeEventSource.instances).toHaveLength(4);
  });

  it('arms again when the shell could not move the stream', async () => {
    const shell = await handoverShell();
    shell.call.mockImplementation((method: string) =>
      Promise.resolve(method === 'direct.handover' ? { closed: 0, kept: 0, skipped: 'other_documents' } : null),
    );
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    emitHandshake(2);

    // The next resume after the spacing tries again; it is still direct.
    await vi.advanceTimersByTimeAsync(30_000);
    shell.call.mockImplementation((method: string) =>
      Promise.resolve(method === 'direct.handover' ? { closed: 1, kept: 0 } : null),
    );
    shell.emit({ preferred: 'direct', sequence: 2, closure: 0 });
    shell.emit({ preferred: 'tunnel', sequence: 3, closure: 0 });
    shell.emit({ preferred: 'direct', sequence: 4, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(2);
  });

  it('hands over a first stream that says it came in another way while direct is preferred', async () => {
    const shell = installDirectShell({ direct: 2 });
    await mountDirectPage();
    await shell.answer(0, 0, 1, 'direct');
    // The stream opened with the baseline; nothing happens before it says how
    // it came in.
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(shell.handovers()).toBe(0);
    FakeEventSource.latest().emit('connected', {
      sub_id: 1, interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS, via: 'other',
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });

  it('leaves a first stream alone that already came in directly', async () => {
    const shell = installDirectShell({ direct: 2 });
    await mountDirectPage();
    await shell.answer(0, 0, 1, 'direct');
    FakeEventSource.latest().emit('connected', {
      sub_id: 1, interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS, via: 'direct',
    });
    await vi.advanceTimersByTimeAsync(11_000);
    expect(shell.handovers()).toBe(0);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('does not hand over a stream that already came in directly, or while a same-origin frame is open', async () => {
    const shell = await handoverShell();
    const { setLiveStreamVia } = await import('../lib/liveStreamVia');
    setLiveStreamVia('direct');
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(0);
    setLiveStreamVia('other');

    const frame = document.createElement('iframe');
    frame.setAttribute('src', '/show/ses_x/');
    document.body.appendChild(frame);
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 0 });
    shell.emit({ preferred: 'direct', sequence: 3, closure: 0 });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(shell.handovers()).toBe(0);
    frame.remove();
  });

  it('counts every same-origin fetch until its body ends', async () => {
    const original = vi.fn(async () => new Response('done'));
    vi.stubGlobal('fetch', original);
    await handoverShell();
    const { pageConnectionsQuiet } = await import('../lib/pageConnections');
    expect(pageConnectionsQuiet()).toBe(true);
    const pending = window.fetch('/api/slow');
    expect(pageConnectionsQuiet()).toBe(false);
    const response = await pending;
    // The headers arrived; the body has not been read yet.
    expect(pageConnectionsQuiet()).toBe(false);
    expect(await response.text()).toBe('done');
    await vi.advanceTimersByTimeAsync(0);
    expect(pageConnectionsQuiet()).toBe(true);
    // Another origin is not this page's connection.
    void window.fetch('https://elsewhere.example/x');
    expect(pageConnectionsQuiet()).toBe(true);
  });

  it('holds new requests while the shell closes quiet connections', async () => {
    const original = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', original);
    const shell = installDirectShell({ direct: 2 });
    let answer: (value: unknown) => void = () => {};
    const status = shell.call.getMockImplementation();
    shell.call.mockImplementation((method: string) =>
      method === 'direct.handover'
        ? new Promise((resolve) => { answer = resolve; })
        : status!(method),
    );
    await mountDirectPage();
    await shell.answer(0, 0);
    emitHandshake(1);
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);

    // A request started during the hand-over waits for its answer.
    const request = window.fetch('/api/refresh');
    await vi.advanceTimersByTimeAsync(1_000);
    expect(original).not.toHaveBeenCalled();
    answer({ closed: 1, kept: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(original).toHaveBeenCalledTimes(1);
    await request;
  });

  it('counts requests from the moment the provider module loads, before anything mounts', async () => {
    const original = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', original);
    installDirectShell({ direct: 2 });
    vi.resetModules();
    await import('./ApiContext');
    const { pageConnectionsQuiet } = await import('../lib/pageConnections');
    const early = window.fetch('/api/session');
    expect(pageConnectionsQuiet()).toBe(false);
    await early;
    await vi.advanceTimersByTimeAsync(0);
    expect(pageConnectionsQuiet()).toBe(true);
  });

  // A shell that reports its path (`direct` version 3).

  const pathShell = async (platform: 'ios' | 'desktop' = 'ios') => {
    const shell = installDirectShell({ direct: 3 }, platform);
    await mountDirectPage();
    await shell.answer(0, 0);
    emitHandshake(1);
    emitControllerLeg(true);
    return shell;
  };

  it('never hands over while the shell is on the relay, and does once it reaches a direct address', async () => {
    const shell = await pathShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'relay' });
    await vi.advanceTimersByTimeAsync(11_000);
    expect(shell.handovers()).toBe(0);
    expect(FakeEventSource.instances).toHaveLength(1);
    // A path the shell cannot name is not a direct address either.
    shell.emit({ preferred: 'direct', sequence: 2, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(0);

    shell.emit({ preferred: 'direct', sequence: 3, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  // The desktop app answers `direct` in a window of another computer's
  // instance, the main view and an app window alike: each is a document of
  // its own with this same bridge.
  it('in the desktop app, hands over on a direct address, and never on the relay or back on the tunnel', async () => {
    const shell = await pathShell('desktop');
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'relay' });
    await vi.advanceTimersByTimeAsync(11_000);
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 0 });
    await vi.advanceTimersByTimeAsync(11_000);
    expect(shell.handovers()).toBe(0);
    expect(FakeEventSource.instances).toHaveLength(1);

    shell.emit({ preferred: 'direct', sequence: 3, closure: 0, path: 'ipv6' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it('keeps the short spacing when the path flaps between the public one and a direct address', async () => {
    const shell = await pathShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    // Each flap arrives at the direct address again: none is followed
    // sooner than the short spacing after the last hand-over.
    for (let sequence = 2; sequence < 12; sequence += 2) {
      shell.emit({ preferred: 'tunnel', sequence, closure: 0 });
      shell.emit({ preferred: 'direct', sequence: sequence + 1, closure: 0, path: 'ipv4' });
      await vi.advanceTimersByTimeAsync(0);
    }
    await vi.advanceTimersByTimeAsync(4_000);
    expect(shell.handovers()).toBe(1);
    await vi.advanceTimersByTimeAsync(1_000);
    shell.emit({ preferred: 'tunnel', sequence: 12, closure: 0 });
    shell.emit({ preferred: 'direct', sequence: 13, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(2);
  });

  it('checks the path again after waiting for a quiet page, and keeps its arm for the next direct address', async () => {
    const shell = await pathShell();
    const { beginPageRequest } = await import('../lib/pageConnections');
    const done = beginPageRequest();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(2_000);
    // While the page was busy the shell went back to the relay.
    shell.emit({ preferred: 'direct', sequence: 2, closure: 0, path: 'relay' });
    done();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(shell.handovers()).toBe(0);
    expect(FakeEventSource.instances).toHaveLength(1);

    // The arm was not spent: the next direct address hands over.
    shell.emit({ preferred: 'direct', sequence: 3, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
  });

  it('follows each arrival at a direct address after the short spacing, and repeats on it only after the long one', async () => {
    const shell = await pathShell();
    // The shell keeps a busy connection every time, so each hand-over arms again.
    shell.call.mockImplementation((method: string) =>
      Promise.resolve(method === 'direct.handover' ? { closed: 0, kept: 1 } : null),
    );
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv6' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);

    // Lost and back, first over the relay: nothing. Then a direct address,
    // a second after the last hand-over: not yet.
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 1 });
    await vi.advanceTimersByTimeAsync(0);
    shell.emit({ preferred: 'direct', sequence: 3, closure: 1, path: 'relay' });
    await vi.advanceTimersByTimeAsync(1_000);
    shell.emit({ preferred: 'direct', sequence: 4, closure: 1, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    // From the relay to the address again, once the short spacing has
    // passed: followed at once, whatever went before in this stretch.
    await vi.advanceTimersByTimeAsync(4_000);
    shell.emit({ preferred: 'direct', sequence: 5, closure: 1, path: 'relay' });
    shell.emit({ preferred: 'direct', sequence: 6, closure: 1, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(2);

    // On the same address, a stream that still came in another way: armed,
    // and held to the long spacing.
    emitHandshakeVia(9, 'other');
    await vi.advanceTimersByTimeAsync(29_000);
    expect(shell.handovers()).toBe(2);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(shell.handovers()).toBe(3);
  });

  it('follows an arrival after the short spacing though a retry was already set for later', async () => {
    const shell = await pathShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    // The stream still came in another way: a repeat, set for the long spacing.
    emitHandshakeVia(2, 'other');
    await vi.advanceTimersByTimeAsync(1_000);
    // The shell goes to the relay and arrives at the address again.
    shell.emit({ preferred: 'direct', sequence: 2, closure: 0, path: 'relay' });
    shell.emit({ preferred: 'direct', sequence: 3, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(3_000);
    expect(shell.handovers()).toBe(1);
    // By itself when the short spacing ends, not when the long one would.
    await vi.advanceTimersByTimeAsync(1_000);
    expect(shell.handovers()).toBe(2);
  });

  it('follows the path again after a return from the background, when the shell could not move it before', async () => {
    const shell = await pathShell();
    // Another page of the instance was open: the shell moved nothing.
    shell.call.mockImplementation((method: string) =>
      Promise.resolve(method === 'direct.handover' ? { closed: 0, kept: 0, skipped: 'other_documents' } : null),
    );
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv6' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    emitHandshakeVia(2, 'other');
    emitControllerLeg(true);

    // The App goes to the background and comes back: its listener closes and
    // binds again, so its preference goes to the public path and returns to
    // the same address. The page is told on the return, by the status it
    // reads and by the events that follow.
    await vi.advanceTimersByTimeAsync(20_000);
    shell.call.mockImplementation((method: string) =>
      Promise.resolve(method === 'direct.handover' ? { closed: 1, kept: 0 } : null),
    );
    setVisibility('hidden');
    setVisibility('visible');
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 1 });
    await vi.advanceTimersByTimeAsync(0);
    emitHandshakeVia(3, 'other');
    emitControllerLeg(true);
    shell.emit({ preferred: 'direct', sequence: 3, closure: 1, path: 'ipv6' });
    await vi.advanceTimersByTimeAsync(0);
    // An arrival: followed at once, 20 s after the last hand-over, and not
    // held to the 30 s a repeat on the same address waits.
    expect(shell.handovers()).toBe(2);
    emitHandshakeVia(4, 'direct');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(shell.handovers()).toBe(2);
  });

  it('makes at most six hand-overs however the path flaps between kinds, until ten minutes pass without one', async () => {
    const shell = await pathShell();
    let sequence = 0;
    const arrive = async (path: 'ipv4' | 'ipv6') => {
      shell.emit({ preferred: 'direct', sequence: sequence += 1, closure: 0, path: 'relay' });
      shell.emit({ preferred: 'direct', sequence: sequence += 1, closure: 0, path });
      await vi.advanceTimersByTimeAsync(0);
    };
    for (let round = 1; round <= 9; round += 1) {
      await arrive(round % 2 === 0 ? 'ipv6' : 'ipv4');
      // A new kind of path each time: the total holds all the same.
      expect(shell.handovers()).toBe(Math.min(round, 6));
      await vi.advanceTimersByTimeAsync(5_000);
    }
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    await arrive('ipv4');
    expect(shell.handovers()).toBe(7);
  });

  // The hand-over comes back by itself, within bounds (ui/src/context/ApiContext.tsx,
  // DIRECT_HANDOVER_MAX and DIRECT_HANDOVER_MAX_FRUITLESS).

  it('comes back when the spacing ends, while the stream still says it came in another way', async () => {
    const shell = await pathShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    // The rebuilt stream landed on a public connection again. No event of
    // the shell follows: the page tries again by itself, at the spacing.
    emitHandshakeVia(2, 'other');
    await vi.advanceTimersByTimeAsync(29_000);
    expect(shell.handovers()).toBe(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(shell.handovers()).toBe(2);
    expect(FakeEventSource.instances).toHaveLength(3);
  });

  it('stays on direct: a stream that came in directly is not handed over again', async () => {
    const shell = await pathShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    emitHandshakeVia(2, 'direct');
    // The shell verifies its path again and again; the page hears each one.
    for (let sequence = 2; sequence < 22; sequence += 2) {
      shell.emit({ preferred: 'tunnel', sequence, closure: 0 });
      shell.emit({ preferred: 'direct', sequence: sequence + 1, closure: 0, path: 'ipv4' });
      await vi.advanceTimersByTimeAsync(31_000);
      // The stream keeps proving itself, so nothing else replaces it.
      emitHeartbeat();
    }
    expect(shell.handovers()).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it('stops after three hand-overs in a row that did not move the stream, until the path changes', async () => {
    const shell = await pathShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    for (let round = 1; round <= 3; round += 1) {
      expect(shell.handovers()).toBe(round);
      emitHandshakeVia(round + 1, 'other');
      await vi.advanceTimersByTimeAsync(30_000);
    }
    // Three in a row left the stream where it was: no fourth.
    expect(shell.handovers()).toBe(3);
    // Not for a stream that connects again, not for the shell verifying its
    // path (the same kind of path afterwards), however long it lasts.
    for (let sequence = 2; sequence < 12; sequence += 2) {
      emitHandshakeVia(10 + sequence, 'other');
      shell.emit({ preferred: 'tunnel', sequence, closure: 0 });
      shell.emit({ preferred: 'direct', sequence: sequence + 1, closure: 0, path: 'ipv4' });
      await vi.advanceTimersByTimeAsync(40_000);
    }
    expect(shell.handovers()).toBe(3);
    expect(FakeEventSource.instances).toHaveLength(4);

    // Another kind of path is a new path: counted from the start.
    shell.emit({ preferred: 'direct', sequence: 12, closure: 0, path: 'relay' });
    shell.emit({ preferred: 'direct', sequence: 13, closure: 0, path: 'ipv6' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(4);
  });

  it('makes at most six hand-overs on one path, however often the stream falls back', async () => {
    const shell = await pathShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    for (let made = 1; made <= 6; made += 1) {
      expect(shell.handovers()).toBe(made);
      // It worked, and later the stream is back on a public connection.
      emitHandshakeVia(made * 2, 'direct');
      await vi.advanceTimersByTimeAsync(5_000);
      emitHandshakeVia(made * 2 + 1, 'other');
      // Never sooner than the spacing.
      await vi.advanceTimersByTimeAsync(20_000);
      expect(shell.handovers()).toBe(made);
      await vi.advanceTimersByTimeAsync(5_000);
    }
    expect(shell.handovers()).toBe(6);
    emitHandshakeVia(20, 'other');
    await vi.advanceTimersByTimeAsync(120_000);
    expect(shell.handovers()).toBe(6);

    // Ten minutes without a hand-over: the counts start again.
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    emitHandshakeVia(21, 'other');
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(7);
  });

  it('hands a busy page over when it goes quiet, and closes nothing meanwhile', async () => {
    const shell = await pathShell();
    const first = FakeEventSource.latest();
    emitHandshakeVia(1, 'other');
    const { beginPageRequest } = await import('../lib/pageConnections');
    const done = beginPageRequest();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(9_000);
    expect(shell.handovers()).toBe(0);
    expect(first.closed).toBe(false);
    // Within a quarter of a second of the request ending, in the first
    // seconds of the wait.
    done();
    await vi.advanceTimersByTimeAsync(250);
    expect(shell.handovers()).toBe(1);
    expect(first.closed).toBe(true);
  });

  it('never retries for a stream that has not said how it came in', async () => {
    const shell = await pathShell();
    // The shell keeps a busy connection, so the hand-over stays armed.
    shell.call.mockImplementation((method: string) =>
      Promise.resolve(method === 'direct.handover' ? { closed: 0, kept: 1 } : null),
    );
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    // An instance that does not report `via`: unknown is not "not direct".
    emitHandshake(2);
    shell.emit({ preferred: 'direct', sequence: 2, closure: 0, path: 'ipv4' });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(shell.handovers()).toBe(1);
  });

  it('does not retry once the shell prefers the public path again, with a shell that reports no path', async () => {
    // `direct` version 2: no path, so nothing but the preference gates it.
    const shell = await handoverShell();
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(1);
    // The rebuilt stream came in another way, and the retry is set.
    emitHandshakeVia(2, 'other');
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 0 });
    shell.emit({ preferred: 'direct', sequence: 3, closure: 0 });
    await vi.advanceTimersByTimeAsync(4_000);
    expect(shell.handovers()).toBe(1);
    // The preference goes to the public path before the short spacing ends.
    shell.emit({ preferred: 'tunnel', sequence: 4, closure: 0 });
    for (let waited = 0; waited < 60_000; waited += 20_000) {
      await vi.advanceTimersByTimeAsync(20_000);
      // The stream keeps proving itself, so nothing else replaces it.
      emitHeartbeat();
    }
    expect(shell.handovers()).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(2);

    // Direct again: handed over, as the short spacing has long passed.
    shell.emit({ preferred: 'direct', sequence: 5, closure: 0 });
    await vi.advanceTimersByTimeAsync(0);
    expect(shell.handovers()).toBe(2);
  });

  it('never hands over without direct version 2', async () => {
    const shell = installDirectShell({ direct: 1 });
    await mountDirectPage();
    await shell.answer(0, 0);
    emitHandshake(1);
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    await vi.advanceTimersByTimeAsync(11_000);
    expect(shell.handovers()).toBe(0);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('rebuilds a healthy stream once per closure, and never for a change of preferred path', async () => {
    const shell = installDirectShell();
    await mountDirectPage();
    await shell.answer(0, 0);
    emitHandshake(1);
    emitControllerLeg(true);
    emitHeartbeat();
    const first = FakeEventSource.latest();
    expect(onConnected).toHaveBeenCalledTimes(1);

    // The preferred path changes. Nothing was closed: the stream stays.
    shell.emit({ preferred: 'direct', sequence: 1, closure: 0 });
    expect(first.closed).toBe(false);
    expect(FakeEventSource.instances).toHaveLength(1);

    // The shell closed connections because their path was lost. It cannot
    // say whether this stream was one of them, so it is replaced now, not
    // after a backoff, and the replacement announces the gap.
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 1 });
    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(localStore.noteLocalStoreStreamState).toHaveBeenLastCalledWith(false);
    expect(onConnected).toHaveBeenCalledTimes(1);
    emitHandshake(2);
    emitControllerLeg(true);
    expect(onConnected).toHaveBeenCalledTimes(2);

    // The same closure again, late or duplicated: nothing more.
    shell.emit({ preferred: 'tunnel', sequence: 2, closure: 1 });
    shell.emit({ preferred: 'direct', sequence: 1, closure: 1 });
    vi.advanceTimersByTime(WORKBENCH_EVENT_RETRY_INITIAL_MS * 2);
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it('opens the replacement when a hidden page returns after a closure', async () => {
    const shell = installDirectShell();
    await mountDirectPage();
    await shell.answer(0, 0);
    emitHandshake(1);
    emitControllerLeg(true);
    const pinned = FakeEventSource.latest();

    setVisibility('hidden');
    shell.emit({ preferred: 'tunnel', sequence: 1, closure: 1 });
    expect(pinned.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(1);

    setVisibility('visible');
    expect(FakeEventSource.instances).toHaveLength(2);
  });
});
