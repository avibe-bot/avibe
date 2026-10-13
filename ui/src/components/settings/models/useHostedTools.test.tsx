// @vitest-environment jsdom
// How the hosted-tools report is ordered against its writes. Each write answers
// the whole status, so what is on screen must be the newest state the server
// committed, whatever order the requests happen to return in.
import * as React from 'react';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { modelsApi } from './modelsApi';
import { foldRegionRead, type RegionRead } from './regionRead';
import type { HostedToolsStatus } from './types';
import { useHostedTools } from './useHostedTools';

const status = (enabled: boolean): HostedToolsStatus => ({
  tools: [{ name: 'web_search', enabled, executor: null, bound: null, candidates: [] }],
  reverify: { engine_upgraded: 0, aged: 0 },
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};

const shown = (read: RegionRead<HostedToolsStatus>) => foldRegionRead<HostedToolsStatus, boolean | null>(read, {
  loading: () => null,
  ready: (data) => data.tools[0].enabled,
  unread: () => null,
  degraded: (data) => data.tools[0].enabled,
});

const wrapper: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <ToastProvider><I18nextProvider i18n={i18n}>{children}</I18nextProvider></ToastProvider>
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('useHostedTools ordering', () => {
  it('MH-HOSTED-UI-007: a read asked for during a write waits, so it cannot paint the pre-write state over the write', async () => {
    const get = vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    const put = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'putHostedTool').mockReturnValue(put.promise);
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let saving!: Promise<void>;
    act(() => { saving = result.current.save('web_search', { enabled: true, executor: null }); });
    // The tab is left and reopened while the PUT is still out.
    await act(async () => { await result.current.refresh(); });
    expect(get).toHaveBeenCalledTimes(1);

    get.mockResolvedValue(status(true));
    await act(async () => { put.resolve(status(true)); await saving; });
    expect(shown(result.current.read)).toBe(true);
    // The held read runs once the write has settled, and agrees with it.
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(shown(result.current.read)).toBe(true);
  });

  it('runs writes one at a time, so each answer lands in the order the server committed them', async () => {
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    const first = deferred<HostedToolsStatus>();
    const second = deferred<HostedToolsStatus>();
    const put = vi.spyOn(modelsApi, 'putHostedTool').mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let writes!: Promise<void>[];
    act(() => {
      writes = [
        result.current.save('web_search', { enabled: true, executor: null }),
        result.current.save('x_search', { enabled: true, executor: null }),
      ];
    });
    // The second PUT has not left: had both, the server could commit them in
    // either order and the client would have no way to tell which answer is newer.
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    await act(async () => { first.resolve(status(true)); await writes[0]; });
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    await act(async () => { second.resolve(status(false)); await writes[1]; });
    // The screen holds the answer to the last write the server committed.
    expect(shown(result.current.read)).toBe(false);
  });

  it('drops a read that was already out when a write started', async () => {
    const first = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'getHostedTools').mockReturnValueOnce(first.promise);
    vi.spyOn(modelsApi, 'putHostedTool').mockResolvedValue(status(true));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });

    await act(async () => { await result.current.save('web_search', { enabled: true, executor: null }); });
    expect(shown(result.current.read)).toBe(true);
    // The read cannot tell whether it saw the write, so its answer never lands.
    await act(async () => { first.resolve(status(false)); await first.promise; });
    expect(shown(result.current.read)).toBe(true);
  });

  it('re-reads after the newest write fails, because a lost answer may have committed', async () => {
    const get = vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    vi.spyOn(modelsApi, 'putHostedTool').mockRejectedValue(new Error('offline'));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));

    get.mockResolvedValue(status(true));
    await act(async () => { await result.current.save('web_search', { enabled: true, executor: null }); });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(shown(result.current.read)).toBe(true);
  });

  it('re-reads when the open Source changes, since an edit can drop its evidence', async () => {
    const get = vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    const { rerender } = renderHook(({ revision }) => useHostedTools(true, revision), {
      wrapper,
      initialProps: { revision: 'source-a' },
    });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    rerender({ revision: 'source-a-with-a-new-key' });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });
});
