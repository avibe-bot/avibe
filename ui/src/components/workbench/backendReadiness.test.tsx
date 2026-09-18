/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
// The wire is the only stand-in. The api the reader consumes is the real one —
// the provider's own `getBackendConnection`, its path, its error policy — so
// this file pins the reader against the producer as shipped rather than against
// a second description of it.
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));

import { ApiProvider, type BackendConnectionState } from '../../context/ApiContext';
import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { useBackendReadiness } from './backendReadiness';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const json = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

/**
 * The connection endpoint with its answers held: a read is outstanding until the
 * test answers it, which is how a route change can be made to happen while an
 * earlier question is still on the wire.
 */
const connectionServer = () => {
  const outstanding: Array<{
    backend: string;
    answer: (state?: Partial<BackendConnectionState>) => void;
    reject: (status?: number) => void;
  }> = [];
  apiFetch.mockImplementation((path: string) => {
    const asked = /^\/api\/backend\/([^/]+)\/connection$/.exec(path);
    // Anything else the provider does on mount is not what this file is about.
    if (!asked) return Promise.resolve(json({}));
    const backend = asked[1];
    return new Promise<Response>((resolve) => {
      outstanding.push({
        backend,
        answer: (state) => resolve(json({
          ok: true,
          backend,
          installed: true,
          enabled: true,
          auth: 'api_key',
          application: 'applied',
          ready: true,
          entry_eligible: true,
          ...state,
        })),
        reject: (status = 403) => resolve(json({ error: 'fixture refusal' }, status)),
      });
    });
  });
  return {
    outstanding,
    /** Which backends were actually asked about, in order. */
    asked: () => apiFetch.mock.calls
      .map(([path]) => /^\/api\/backend\/([^/]+)\/connection$/.exec(String(path))?.[1])
      .filter((backend): backend is string => Boolean(backend)),
  };
};

const Probe = ({ backend, asked }: { backend: string | null; asked: boolean }) => {
  const readiness = useBackendReadiness(backend, asked);
  return <span data-testid="observed">{readiness ? `${readiness.backend}:${readiness.ready}` : 'nothing'}</span>;
};

const tree = (backend: string | null, asked: boolean) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <ApiProvider>
        <Probe backend={backend} asked={asked} />
      </ApiProvider>
    </ToastProvider>
  </I18nextProvider>
);

const renderProbe = (backend: string | null = 'codex', asked = true) => {
  const view = render(tree(backend, asked));
  return { ...view, ask: (next: string | null, stillAsked = true) => view.rerender(tree(next, stillAsked)) };
};

const observed = () => screen.getByTestId('observed').textContent;

beforeEach(() => {
  apiFetch.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('useBackendReadiness', () => {
  it('repeats what the backend said about itself, from the endpoint that owns it', async () => {
    const server = connectionServer();
    renderProbe('codex');

    await waitFor(() => expect(server.outstanding).toHaveLength(1));
    // A plain read of the producer's own route — no start, no auth, no write.
    expect(apiFetch.mock.calls.map(([path, init]) => [path, init?.method ?? 'GET']))
      .toContainEqual(['/api/backend/codex/connection', 'GET']);

    await act(async () => { server.outstanding[0].answer(); });

    expect(observed()).toBe('codex:true');
  });

  it.each([
    ['the backend does not call itself ready', { ready: false }],
    ['the read itself did not succeed', { ok: false }],
    ['the answer is about a different backend', { backend: 'claude' }],
    ['the backend is waiting on a permission the user has not given', { ready: false, permission_required: true }],
  ])('observes nothing when %s', async (_case, state) => {
    const server = connectionServer();
    renderProbe('codex');

    await waitFor(() => expect(server.outstanding).toHaveLength(1));
    await act(async () => { server.outstanding[0].answer(state as Partial<BackendConnectionState>); });

    expect(observed()).toBe('nothing');
  });

  it('observes nothing, and stays out of the way, when the read fails', async () => {
    const server = connectionServer();
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderProbe('codex');

    await waitFor(() => expect(server.outstanding).toHaveLength(1));
    await act(async () => { server.outstanding[0].reject(); });

    // A refusal is not a report of "not ready" — it is no report, and the home
    // goes on working. The rejection is the api's own error policy; what matters
    // here is that the reader lets it pass instead of failing the tree with it.
    expect(observed()).toBe('nothing');
    logged.mockRestore();
  });

  it.each([
    ['there is nothing left to announce', 'codex', false],
    ['the home has no backend to ask about', null, true],
    ['the route names a backend this build cannot ask about', 'future-backend', true],
  ])('asks nothing at all when %s', async (_case, backend, asked) => {
    const server = connectionServer();
    renderProbe(backend, asked);

    await act(async () => {});

    expect(server.asked()).toEqual([]);
    expect(observed()).toBe('nothing');
  });

  it('drops an answer to a question the route has moved on from', async () => {
    const server = connectionServer();
    const view = renderProbe('codex');

    await waitFor(() => expect(server.outstanding).toHaveLength(1));
    view.ask('claude');
    await waitFor(() => expect(server.outstanding).toHaveLength(2));

    // Codex answers ready, but the home would no longer run Codex.
    await act(async () => { server.outstanding[0].answer(); });
    expect(observed()).toBe('nothing');

    await act(async () => { server.outstanding[1].answer(); });
    expect(observed()).toBe('claude:true');
  });

  it('does not let an old answer stand in for the same backend asked again', async () => {
    const server = connectionServer();
    const view = renderProbe('codex');

    await waitFor(() => expect(server.outstanding).toHaveLength(1));
    view.ask('claude');
    await waitFor(() => expect(server.outstanding).toHaveLength(2));
    view.ask('codex');
    await waitFor(() => expect(server.outstanding).toHaveLength(3));

    // The first Codex read names the backend now in effect, and is still stale:
    // it answers a question that was withdrawn two route changes ago.
    await act(async () => { server.outstanding[0].answer(); });
    expect(observed()).toBe('nothing');

    await act(async () => { server.outstanding[2].answer(); });
    expect(observed()).toBe('codex:true');
  });

  it('forgets what it observed once the question changes', async () => {
    const server = connectionServer();
    const view = renderProbe('codex');

    await waitFor(() => expect(server.outstanding).toHaveLength(1));
    await act(async () => { server.outstanding[0].answer(); });
    expect(observed()).toBe('codex:true');

    view.ask('claude');
    // Nothing has been observed about Claude yet, and what was true of Codex is
    // not evidence about it.
    expect(observed()).toBe('nothing');

    view.ask('codex', false);
    await act(async () => {});
    expect(observed()).toBe('nothing');
  });

  it('reads once for one question, whatever else the interface does', async () => {
    const server = connectionServer();
    renderProbe('codex');

    await waitFor(() => expect(server.asked()).toEqual(['codex']));
    await act(async () => { server.outstanding[0].answer(); });

    // The provider hands out a new api on a language change. A reader that
    // depended on that identity would re-read the backend every time the
    // interface language moved; this one is asking one question.
    await act(async () => { await i18n.changeLanguage('en-GB'); });
    await act(async () => { await i18n.changeLanguage('en'); });

    expect(server.asked()).toEqual(['codex']);
    expect(observed()).toBe('codex:true');
  });

  it('ignores an answer that arrives after the home is gone', async () => {
    const server = connectionServer();
    const view = renderProbe('codex');

    await waitFor(() => expect(server.outstanding).toHaveLength(1));
    view.unmount();

    await act(async () => { server.outstanding[0].answer(); });

    expect(screen.queryByTestId('observed')).toBeNull();
  });
});
