/* @vitest-environment jsdom */

import { act, cleanup, render, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { SessionDraftLocalCache } from '../lib/sessionDraftLocalCache';
import { ApiProvider, useApi } from './ApiContext';

let capturedApi: ReturnType<typeof useApi> | null = null;

const CaptureApi = () => {
  const api = useApi();
  useEffect(() => {
    capturedApi = api;
  }, [api]);
  return null;
};

beforeEach(() => {
  window.localStorage.clear();
  apiFetch.mockReset();
  showToast.mockReset();
  capturedApi = null;
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
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

  it('bounds a stalled write and releases its reconciled successor', async () => {
    vi.useFakeTimers();
    let putCount = 0;
    apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === 'PUT' && ++putCount === 1) {
        return new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new DOMException('aborted', 'AbortError'));
          }, { once: true });
        });
      }
      if (!init?.method) {
        return new Response(JSON.stringify({ text: '', updated_at: null }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      const body = JSON.parse(String(init.body)) as { text: string };
      return new Response(JSON.stringify({
        ok: true,
        draft: { text: body.text, updated_at: 'second-revision' },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    render(<ApiProvider><CaptureApi /></ApiProvider>);
    capturedApi!.cacheSessionDraft('session-a', 'first');
    const first = capturedApi!.setSessionDraft('session-a', 'first');
    await Promise.resolve();
    capturedApi!.cacheSessionDraft('session-a', 'second');
    const second = capturedApi!.setSessionDraft('session-a', 'second');

    await vi.advanceTimersByTimeAsync(12_000);
    await expect(first).resolves.toMatchObject({ ok: false });
    await expect(second).resolves.toMatchObject({ ok: true });
    const writes = apiFetch.mock.calls
      .filter(([, init]) => init?.method === 'PUT')
      .map(([, init]) => JSON.parse(String(init?.body)));
    expect(writes).toEqual([
      { text: 'first', expected_updated_at: null },
      { text: 'second', expected_updated_at: null },
    ]);
  });

  it('rebases and retries text restored after a rejected send', async () => {
    let resolveDraft!: (response: Response) => void;
    const draft = new Promise<Response>((resolve) => { resolveDraft = resolve; });
    apiFetch.mockImplementation(async (_path: string, init?: RequestInit) => {
      if (!init?.method) return draft;
      const body = JSON.parse(String(init.body)) as { text: string };
      return new Response(JSON.stringify({
        ok: true,
        draft: { text: body.text, updated_at: 'restored-revision' },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    render(<ApiProvider><CaptureApi /></ApiProvider>);
    capturedApi!.cacheSessionDraft('session-a', 'submitted');
    capturedApi!.cacheSessionDraft('session-a', '');
    const recovery = capturedApi!.recoverSessionDraftAfterRejectedSend('session-a');
    capturedApi!.cacheSessionDraft('session-a', 'submitted');
    resolveDraft(new Response(JSON.stringify({ text: '', updated_at: 'clear-revision' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    await recovery;

    const write = apiFetch.mock.calls.find(([, init]) => init?.method === 'PUT');
    expect(JSON.parse(String(write?.[1]?.body))).toEqual({
      text: 'submitted',
      expected_updated_at: 'clear-revision',
    });
  });

  it('rebases text typed while an accepted send advances the draft revision', async () => {
    let resolveStaleWrite!: (response: Response) => void;
    const staleWrite = new Promise<Response>((resolve) => { resolveStaleWrite = resolve; });
    let putCount = 0;
    apiFetch.mockImplementation(async (_path: string, init?: RequestInit) => {
      if (++putCount === 1) return staleWrite;
      const body = JSON.parse(String(init?.body)) as { text: string };
      return new Response(JSON.stringify({
        ok: true,
        draft: { text: body.text, updated_at: 'next-revision' },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    render(<ApiProvider><CaptureApi /></ApiProvider>);
    capturedApi!.cacheSessionDraft('session-a', 'submitted');
    capturedApi!.cacheSessionDraft('session-a', '');
    capturedApi!.cacheSessionDraft('session-a', 'next prompt');
    const stale = capturedApi!.setSessionDraft('session-a', 'next prompt');
    await Promise.resolve();

    await capturedApi!.reconcileSessionDraftAfterSend('session-a', {
      text: '',
      updated_at: 'clear-revision',
    });
    resolveStaleWrite(new Response(JSON.stringify({
      ok: true,
      draft: { text: 'next prompt', updated_at: 'stale-write-revision' },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    await stale;

    const writes = apiFetch.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));
    expect(writes).toEqual([
      { text: 'next prompt', expected_updated_at: null },
      { text: 'next prompt', expected_updated_at: 'clear-revision' },
    ]);
    const cache = new SessionDraftLocalCache(window.localStorage);
    expect(cache.read('session-a')).toMatchObject({
      text: 'next prompt',
      serverUpdatedAt: 'next-revision',
      dirty: false,
    });
  });
});
