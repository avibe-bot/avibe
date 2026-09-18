/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// PERMISSIONS-016 — the Harness page below runtime management. `runs.updated`
// already woke it; `definitions.updated` is admitted at the same Editor tier and
// reduced to the same bare frame, but nothing listened for it by name, so a
// definition created, paused or retired anywhere else left this page stale until
// an unrelated run happened to fire. This drives the REAL stream: a named
// EventSource frame carrying `data: {}`, through the provider's own listener and
// read cache, into the page's refetch -- with no runs event and no reconnect.

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../../lib/apiFetch', () => ({
  apiFetch,
  isApiFetchDeadlineAbort: () => false,
  ensureCsrfToken: async () => 'csrf-not-real',
  withApiDeadline: async <T,>(run: (signal: AbortSignal) => Promise<T>) =>
    run(new AbortController().signal),
  recoverRemoteAuthFromSessionProbe: async () => {},
}));
vi.mock('./CapabilityTabs', () => ({ CapabilityTabs: () => null }));

import en from '../../i18n/en.json';
import { ApiProvider } from '../../context/ApiContext';
import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { ToastContext } from '../../context/ToastContext';
import { capabilitiesFor } from '../../lib/testing/instanceRoleCapabilities';
import { HarnessPage } from './HarnessPage';

/** Enough of EventSource to be fed named frames; jsdom ships none. */
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

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const HARNESS_BOOTSTRAP = '/api/harness/bootstrap';

const jsonFor = (path: string) => {
  if (path.startsWith(HARNESS_BOOTSTRAP)) {
    return {
      counts: { tasks: {}, watches: {}, runs: {} },
      page: { tasks: [], counts: {}, has_more: false },
    };
  }
  if (path.startsWith('/api/workbench/prefs')) return { background_work_banner_enabled: false };
  return { ok: true, agents: [], default_agent_name: null };
};

const harnessReads = () =>
  apiFetch.mock.calls.filter(([path]) => String(path).startsWith(HARNESS_BOOTSTRAP)).length;

/** What the server sends below runtime management: the type, and nothing else. */
const emitBareFrame = (type: string) => FakeEventSource.latest().emit(type, { type, data: {} });

const mountAsEditor = () =>
  render(
    <ToastContext.Provider value={{ showToast: () => {} }}>
      <ApiProvider>
        <I18nextProvider i18n={i18n}>
          <InstanceAuthorizationContext.Provider
            value={{
              remote: true,
              instanceKind: 'organization',
              instanceRole: 'editor',
              capabilities: capabilitiesFor('editor'),
            }}
          >
            <MemoryRouter initialEntries={['/harness']}>
              <HarnessPage />
            </MemoryRouter>
          </InstanceAuthorizationContext.Provider>
        </I18nextProvider>
      </ApiProvider>
    </ToastContext.Provider>,
  );

/** Mount, then complete the handshake a real connect would, with a healthy leg. */
const mountConnected = async () => {
  const view = mountAsEditor();
  await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
  await act(async () => {
    FakeEventSource.latest().emit('connected', { sub_id: 1, interval_ms: 25_000 });
    FakeEventSource.latest().emit('workbench.events.bridge.status', {
      type: 'workbench.events.bridge.status',
      data: { connected: true },
    });
  });
  await waitFor(() => expect(harnessReads()).toBeGreaterThan(0));
  return view;
};

beforeEach(() => {
  apiFetch.mockReset();
  apiFetch.mockImplementation(async (path: string) => ({
    ok: true,
    status: 200,
    json: async () => jsonFor(String(path)),
  }));
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('PERMISSIONS-016 HarnessPage wakes on the bare definitions.updated frame', () => {
  it('refetches the Harness view from the named signal alone', async () => {
    await mountConnected();
    const before = harnessReads();

    await act(async () => emitBareFrame('definitions.updated'));

    // A second network read, not the 1.5s read cache answering itself: the
    // listener has to invalidate `/api/harness` for this to be reachable.
    await waitFor(() => expect(harnessReads()).toBeGreaterThan(before));
    // No runs event, and the same socket throughout -- the refetch came from
    // this frame rather than from an adjacent signal or a reconnect.
    expect(FakeEventSource.instances.length).toBe(1);
  });

  it('keeps waking on runs.updated and stops when the page unmounts', async () => {
    const view = await mountConnected();
    const before = harnessReads();

    await act(async () => emitBareFrame('runs.updated'));
    await waitFor(() => expect(harnessReads()).toBeGreaterThan(before));

    const afterRuns = harnessReads();
    view.unmount();
    await act(async () => {
      if (!FakeEventSource.latest().closed) emitBareFrame('definitions.updated');
    });
    expect(harnessReads()).toBe(afterRuns);
  });
});
