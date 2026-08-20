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

/** Open a stream and complete its handshake, as a real connect would. */
const mountConnectedStream = () => {
  render(<ApiProvider><Subscriber /></ApiProvider>);
  FakeEventSource.latest().emit('connected', { sub_id: 1 });
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
      FakeEventSource.latest().emit('heartbeat', {
        interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
      });
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
});
