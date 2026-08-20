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

const onResumeGap = vi.fn();

const Subscriber = () => {
  const api = useApi();
  useEffect(() => api.connectWorkbenchEvents({ onResumeGap: () => onResumeGap() }), [api]);
  return null;
};

/** Open a stream and complete its handshake, as a real connect would. */
const mountConnectedStream = () => {
  render(<ApiProvider><Subscriber /></ApiProvider>);
  FakeEventSource.latest().emit('connected', { sub_id: 1 });
};

/** The page or the network coming back, which is what the gate is asked about. */
const reactivate = () => {
  window.dispatchEvent(new Event('online'));
};

beforeEach(() => {
  vi.useFakeTimers();
  apiFetch.mockReset();
  showToast.mockReset();
  onResumeGap.mockReset();
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('ApiProvider resume gap', () => {
  it('stays silent when the stream can prove it never stopped', () => {
    mountConnectedStream();

    // The whole optimization: returning to a page whose stream kept running
    // must not make consumers re-read what it already delivered.
    reactivate();
    expect(onResumeGap).not.toHaveBeenCalled();

    // Heartbeats keep proving it across a gap longer than the stale window, so
    // a page away for minutes still returns to a stream it can trust.
    for (let elapsed = 0; elapsed < STALE_AFTER_MS * 2; elapsed += STALE_AFTER_MS - 1_000) {
      vi.advanceTimersByTime(STALE_AFTER_MS - 1_000);
      FakeEventSource.latest().emit('heartbeat', {
        interval_ms: WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
      });
      reactivate();
    }
    expect(onResumeGap).not.toHaveBeenCalled();
  });

  it('hands the gap to consumers when the stream went quiet across it', () => {
    mountConnectedStream();
    const zombie = FakeEventSource.latest();

    // No heartbeat for longer than the stale window. The connection still
    // claims OPEN -- iOS resumes a suspended tab exactly like this, with the
    // socket dead and no error ever delivered -- so `readyState` alone would
    // wrongly vouch for it.
    vi.advanceTimersByTime(STALE_AFTER_MS);
    expect(zombie.readyState).toBe(FakeEventSource.OPEN);

    reactivate();
    expect(onResumeGap).toHaveBeenCalledTimes(1);
  });

  it('does not let a replacement stream inherit the old one as proof of life', () => {
    mountConnectedStream();
    const first = FakeEventSource.latest();

    vi.advanceTimersByTime(STALE_AFTER_MS);
    reactivate();
    expect(onResumeGap).toHaveBeenCalledTimes(1);
    expect(first.closed).toBe(true);

    // The stale stream was recycled; until the replacement completes its own
    // handshake there is nothing vouching for it, so a second return is
    // another gap rather than a stream that proved anything.
    const replacement = FakeEventSource.latest();
    expect(replacement).not.toBe(first);
    reactivate();
    expect(onResumeGap).toHaveBeenCalledTimes(2);

    // Once a stream does connect, it speaks for itself again. Read the current
    // one back rather than reusing the handle: an unproven stream is recycled
    // by the wake, so the previous reactivation replaced it too.
    FakeEventSource.latest().emit('connected', { sub_id: 2 });
    reactivate();
    expect(onResumeGap).toHaveBeenCalledTimes(2);
  });
});
