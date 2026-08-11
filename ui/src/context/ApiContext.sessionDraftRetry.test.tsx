/* @vitest-environment jsdom */

import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { SessionDraftLocalCache } from '../lib/sessionDraftLocalCache';
import { ApiProvider } from './ApiContext';

beforeEach(() => {
  window.localStorage.clear();
  apiFetch.mockReset();
  showToast.mockReset();
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  });
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('ApiProvider session draft reconnect', () => {
  it('syncs restored dirty drafts when a visible provider mounts', async () => {
    const cache = new SessionDraftLocalCache(window.localStorage);
    cache.writeDirty('session-a', 'restored A', 'rev-a');
    apiFetch.mockImplementation(async (_path: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { text: string };
      return new Response(JSON.stringify({
        ok: true,
        draft: { text: body.text, updated_at: 'synced-a' },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    render(<ApiProvider><div /></ApiProvider>);

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));
    expect(apiFetch.mock.calls[0]?.[0]).toBe('/api/sessions/session-a/draft');
    expect(cache.read('session-a')?.dirty).toBe(false);
  });

  it('syncs dirty drafts for every session when the browser comes online', async () => {
    const cache = new SessionDraftLocalCache(window.localStorage);
    cache.writeDirty('session-a', 'offline A', 'rev-a');
    cache.writeDirty('session-b', 'offline B', 'rev-b');
    cache.writeClean('session-c', 'already synced', 'rev-c');
    apiFetch.mockImplementation(async (_path: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as {
        text: string;
        expected_updated_at: string | null;
      };
      return new Response(JSON.stringify({
        ok: true,
        draft: {
          text: body.text,
          updated_at: `synced-${body.expected_updated_at}`,
        },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    render(<ApiProvider><div /></ApiProvider>);
    act(() => window.dispatchEvent(new Event('online')));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));
    const writes = apiFetch.mock.calls.map(([path, init]) => ({
      path,
      body: JSON.parse(String(init?.body)),
    }));
    expect(writes).toEqual(expect.arrayContaining([
      {
        path: '/api/sessions/session-a/draft',
        body: { text: 'offline A', expected_updated_at: 'rev-a' },
      },
      {
        path: '/api/sessions/session-b/draft',
        body: { text: 'offline B', expected_updated_at: 'rev-b' },
      },
    ]));
    expect(cache.read('session-a')?.dirty).toBe(false);
    expect(cache.read('session-b')?.dirty).toBe(false);
    expect(cache.read('session-c')).toMatchObject({ text: 'already synced', dirty: false });
  });
});
