/* @vitest-environment jsdom */

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { DependenciesResult, DependencyItem, DependencyReadOptions } from '@/context/ApiContext';
import { DEPENDENCY_CHECK_GROUPS, useDependencyChecks } from './useDependencyChecks';

const row = (id: string, overrides: Partial<DependencyItem> = {}): DependencyItem => ({
  id, kind: 'runtime', required: true, installed: true, status: 'ready', version: '1.2.3', ...overrides,
});
const ready = ({ ids = [] }: DependencyReadOptions = {}): DependenciesResult => ({
  ok: true, deps: ids.map((id) => row(id)),
});
const deferred = () => {
  let resolve!: (value: DependenciesResult) => void;
  const promise = new Promise<DependenciesResult>((done) => { resolve = done; });
  return { promise, resolve };
};

afterEach(cleanup);

describe('independent dependency checks', () => {
  it('publishes completed groups while another group is still waiting', async () => {
    const slow = deferred();
    const read = vi.fn(async (options: DependencyReadOptions = {}) => (
      options.ids?.includes('askill') ? slow.promise : ready(options)
    ));
    const { result } = renderHook(() => useDependencyChecks(read));

    act(() => { void result.current.refresh(); });
    await waitFor(() => expect(result.current.checks.avault.data?.status).toBe('ready'));
    expect(result.current.checks.askill).toEqual({ data: null, checking: true, error: null });
    for (const ids of DEPENDENCY_CHECK_GROUPS) {
      expect(read).toHaveBeenCalledWith({ ids, signal: expect.any(AbortSignal) });
    }
    expect(read).toHaveBeenCalledTimes(DEPENDENCY_CHECK_GROUPS.length);

    await act(async () => slow.resolve(ready({ ids: ['askill'] })));
    expect(result.current.checking).toBe(false);
  });

  it('rechecks only the owner of a repaired dependency and rejects an older result', async () => {
    const old = deferred();
    const read = vi.fn(async (options: DependencyReadOptions = {}) => ready(options));
    const { result } = renderHook(() => useDependencyChecks(read));
    read.mockImplementationOnce(() => old.promise);
    act(() => { void result.current.refresh('memory-runtime'); });
    const oldSignal = read.mock.calls[0][0]?.signal;

    await act(async () => result.current.refresh('memory-runtime'));
    expect(oldSignal?.aborted).toBe(true);
    const current = result.current.checks['memory-runtime'];
    expect(read.mock.calls.map(([options]) => options?.ids)).toEqual([
      ['memory-package', 'memory-runtime'], ['memory-package', 'memory-runtime'],
    ]);

    await act(async () => old.resolve({
      ok: true,
      deps: ['memory-package', 'memory-runtime'].map((id) => row(id, { installed: false, status: 'error' })),
    }));
    expect(result.current.checks['memory-runtime']).toEqual(current);
  });

  it('keeps prior raw failure evidence when the latest inspection fails', async () => {
    const source = Object.freeze(row('memory-package', {
      installed: false, status: 'error', readiness: 'not_ready', action_class: 'operator_only',
      reason: 'memory_package_source_build',
    }));
    const failure = Object.freeze(row('memory-runtime', {
      installed: false, status: 'error', reason: 'memory_runtime_preparation_import_timeout',
    }));
    const read = vi.fn().mockResolvedValue({ ok: true, deps: [source, failure] });
    const { result } = renderHook(() => useDependencyChecks(read));
    await act(async () => result.current.refresh('memory-runtime'));
    read.mockRejectedValue(new Error('unavailable'));
    await act(async () => result.current.refresh('memory-runtime'));
    expect(result.current.checks['memory-package']).toEqual({ data: source, checking: false, error: 'failed' });
    expect(result.current.checks['memory-runtime']).toEqual({ data: failure, checking: false, error: 'failed' });
  });

  it.each([{ ok: false, deps: [] }, { ok: true, deps: [] }, { ok: true, deps: [row('memory-package')] }])(
    'never treats an incomplete group response as checked: %j', async (response) => {
      const { result } = renderHook(() => useDependencyChecks(vi.fn().mockResolvedValue(response)));
      await act(async () => result.current.refresh('memory-runtime'));
      for (const id of ['memory-package', 'memory-runtime']) {
        expect(result.current.checks[id]).toEqual({ data: null, checking: false, error: 'failed' });
      }
    },
  );

  it('refreshes a startup repair until settled without waiting on another group', async () => {
    vi.useFakeTimers();
    try {
      const slow = deferred();
      let repairing = true;
      const read = vi.fn(async (options: DependencyReadOptions = {}) => (
        options.ids?.includes('askill') ? slow.promise : {
          ...ready(options), reconciling: repairing,
          reconciling_dependencies: repairing ? ['show-runtime'] : [],
        }
      ));
      const { result, unmount } = renderHook(() => useDependencyChecks(read));
      act(() => { void result.current.refresh(); });
      await act(async () => { await Promise.resolve(); });
      expect(result.current.checks.askill.checking).toBe(true);
      expect(result.current.checks['show-runtime'].reconciling).toBe(true);
      repairing = false;
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      expect(result.current.checks['show-runtime'].reconciling).toBe(false);
      const calls = read.mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
      expect(read).toHaveBeenCalledTimes(calls);
      unmount();
      await act(async () => slow.resolve(ready({ ids: ['askill'] })));
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels every owned request on unmount', async () => {
    const slow = deferred();
    const read = vi.fn((_options?: DependencyReadOptions) => slow.promise);
    const { result, unmount } = renderHook(() => useDependencyChecks(read));
    act(() => { void result.current.refresh(); });
    unmount();
    for (const [options] of read.mock.calls) expect(options?.signal?.aborted).toBe(true);
    await act(async () => slow.resolve({ ok: true, deps: [] }));
  });
});
