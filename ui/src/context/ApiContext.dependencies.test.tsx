/* @vitest-environment jsdom */

import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('../lib/apiFetch', async (loadOriginal) => ({
  ...await loadOriginal<typeof import('../lib/apiFetch')>(), apiFetch,
}));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

import { ApiProvider, useApi } from './ApiContext';
import { isApiFetchDeadlineAbort } from '../lib/apiFetch';
import { DEPENDENCY_CHECK_GROUPS } from '../components/settings/useDependencyChecks';

beforeEach(() => { apiFetch.mockReset(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('dependency inspection transport', () => {
  it.each([true, false])(
    'preserves each requested group and the default full check (URLSearchParams.size available: %s)',
    async (sizeAvailable) => {
      const prototype = URLSearchParams.prototype;
      const size = Object.getOwnPropertyDescriptor(prototype, 'size');
      try {
        if (!sizeAvailable) {
          Reflect.deleteProperty(prototype, 'size');
          expect('size' in new URLSearchParams()).toBe(false);
        }
        apiFetch.mockImplementation(async () => new Response(JSON.stringify({ ok: true, deps: [] })));
        const { result } = renderHook(useApi, { wrapper: ApiProvider });
        for (const ids of DEPENDENCY_CHECK_GROUPS) await result.current.listDependencies({ ids });
        await result.current.listDependencies();
        expect(apiFetch.mock.calls.map(([path]) => new URL(path, 'http://localhost').searchParams.getAll('id')))
          .toEqual([...DEPENDENCY_CHECK_GROUPS, []]);
        expect(apiFetch).toHaveBeenLastCalledWith('/api/dependencies', { signal: expect.any(AbortSignal) });
      } finally {
        if (size) Object.defineProperty(prototype, 'size', size);
      }
    },
  );

  it.each(['fetch', 'body'] as const)('bounds the complete %s phase with one deadline', async (phase) => {
    const { result } = renderHook(useApi, { wrapper: ApiProvider });
    vi.useFakeTimers();
    apiFetch.mockImplementation((_path: string, { signal }: RequestInit) => {
      const waiting = () => new Promise((_resolve, reject) => {
        signal!.addEventListener('abort', () => reject(signal!.reason), { once: true });
      });
      return phase === 'fetch' ? waiting() : Promise.resolve({ ok: true, json: waiting });
    });
    const outcome = result.current.listDependencies().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(isApiFetchDeadlineAbort(await outcome)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('honors caller cancellation without labeling it a timeout', async () => {
    const { result } = renderHook(useApi, { wrapper: ApiProvider });
    vi.useFakeTimers();
    apiFetch.mockImplementation((_path: string, { signal }: RequestInit) => new Promise((_resolve, reject) => {
      signal!.addEventListener('abort', () => reject(signal!.reason), { once: true });
    }));
    const caller = new AbortController();
    const outcome = result.current.listDependencies({ ids: ['askill'], signal: caller.signal })
      .catch((error: unknown) => error);
    caller.abort();
    expect(await outcome).toBe(caller.signal.reason);
    expect(isApiFetchDeadlineAbort(await outcome)).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('Model Hub engine update operations', () => {
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
  const paths = () => apiFetch.mock.calls.map(([path, init]) => `${(init as RequestInit | undefined)?.method ?? 'GET'} ${path}`);

  it('starts a check with an empty body and polls its receipt until it settles', async () => {
    vi.useFakeTimers();
    const answers = [
      json({ ok: true, job_id: 'engine-1', operation_id: 'engine-1', kind: 'check', status: 'running', update_state: 'checking' }, 202),
      json({ ok: true, job_id: 'engine-1', kind: 'check', status: 'running', update_state: 'checking' }),
      json({ ok: true, job_id: 'engine-1', kind: 'check', status: 'succeeded', update_state: 'available' }),
    ];
    apiFetch.mockImplementation(async () => answers.shift());
    const { result } = renderHook(useApi, { wrapper: ApiProvider });
    const outcome = result.current.runEngineOperation('check');
    await vi.advanceTimersByTimeAsync(2_000);
    expect(await outcome).toMatchObject({ ok: true, status: 'succeeded', update_state: 'available', outcome: 'settled' });
    expect(paths()).toEqual([
      'POST /api/dependencies/model-hub-engine/check',
      'GET /api/dependencies/model-hub-engine/install/engine-1',
      'GET /api/dependencies/model-hub-engine/install/engine-1',
    ]);
    expect(apiFetch.mock.calls[0][1].body).toBe('{}');
  });

  it('returns a refusal as a result, not a thrown error or a finished operation', async () => {
    apiFetch.mockImplementation(async () => json({ ok: false, error: 'controller_unavailable', update_state: 'waiting_for_controller' }, 503));
    const { result } = renderHook(useApi, { wrapper: ApiProvider });
    expect(await result.current.runEngineOperation('update')).toEqual({
      ok: false, error: 'controller_unavailable', update_state: 'waiting_for_controller', outcome: 'refused',
    });
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it('waits out a running check and never sends the update a second time by itself', async () => {
    vi.useFakeTimers();
    const answers = [
      json({ ok: false, error: 'operation_busy', operation_id: 'engine-check' }, 409),
      json({ ok: true, job_id: 'engine-check', kind: 'check', status: 'succeeded', update_state: 'available' }),
    ];
    apiFetch.mockImplementation(async () => answers.shift());
    const { result } = renderHook(useApi, { wrapper: ApiProvider });
    const outcome = result.current.runEngineOperation('update');
    await vi.advanceTimersByTimeAsync(2_000);
    expect(await outcome).toMatchObject({ job_id: 'engine-check', kind: 'check', status: 'succeeded', outcome: 'settled' });
    expect(paths()).toEqual([
      'POST /api/dependencies/model-hub-engine/update',
      'GET /api/dependencies/model-hub-engine/install/engine-check',
    ]);
  });

  it('reports a receipt the owner no longer has as unknown, not as a failure', async () => {
    vi.useFakeTimers();
    const answers = [
      json({ ok: true, job_id: 'engine-9', kind: 'update', status: 'running' }, 202),
      json({ ok: false, error: 'job_not_found' }, 404),
    ];
    apiFetch.mockImplementation(async () => answers.shift());
    const { result } = renderHook(useApi, { wrapper: ApiProvider });
    const outcome = result.current.runEngineOperation('update');
    await vi.advanceTimersByTimeAsync(1_000);
    expect(await outcome).toEqual({ ok: false, error: 'job_not_found', outcome: 'unknown' });
    expect(apiFetch).toHaveBeenCalledTimes(2);
  });

  it('reports an operation still running when the wait runs out as unknown', async () => {
    vi.useFakeTimers();
    apiFetch.mockImplementation(async (_path: string, init?: RequestInit) => (init?.method === 'POST'
      ? json({ ok: true, job_id: 'engine-slow', kind: 'update', status: 'running' }, 202)
      : json({ ok: true, job_id: 'engine-slow', kind: 'update', status: 'running', update_state: 'updating' })));
    const { result } = renderHook(useApi, { wrapper: ApiProvider });
    const outcome = result.current.runEngineOperation('update');
    await vi.advanceTimersByTimeAsync(431_000);
    expect(await outcome).toMatchObject({ status: 'running', outcome: 'unknown' });
  });

  it('sends the automatic-update switch as one boolean', async () => {
    apiFetch.mockImplementation(async () => json({ ok: true, auto_update: false }));
    const { result } = renderHook(useApi, { wrapper: ApiProvider });
    expect(await result.current.setEngineAutoUpdate(false)).toEqual({ ok: true, auto_update: false });
    expect(apiFetch.mock.calls[0][0]).toBe('/api/dependencies/model-hub-engine/auto-update');
    expect(apiFetch.mock.calls[0][1].body).toBe('{"enabled":false}');
  });
});
