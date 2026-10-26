// @vitest-environment jsdom
// How the hosted-tools report is ordered against its writes. Each write answers
// the whole status, so what is on screen must be the newest state the server
// committed, whatever order the requests happen to return in.
import * as React from 'react';
import { act, cleanup, renderHook, screen, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { ApiCallError, modelsApi } from './modelsApi';
import { foldRegionRead, type RegionRead } from './regionRead';
import type { HostedToolsStatus } from './types';
import { useHostedTools } from './useHostedTools';

const status = (enabled: boolean): HostedToolsStatus => ({
  tools: [{
    name: 'web_search',
    prefer_native: true, services: {}, service_order: [],
    enabled,
    status: { latest_ok: null, verified: [], refused: [] },
    executor_order: null,
    effective_order: [],
    executor_candidates: [],
  }],
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
  vi.unstubAllGlobals();
});

describe('useHostedTools ordering', () => {
  it('MH-HOSTED-UI-007: a read asked for during a write waits, so it cannot paint the pre-write state over the write', async () => {
    const get = vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    const put = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'putHostedTool').mockReturnValue(put.promise);
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let saving!: Promise<void>;
    act(() => { saving = result.current.save('web_search', { enabled: true }); });
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

  it('MH-HOSTED-UI-007: runs writes one at a time, so each answer lands in the order the server committed them', async () => {
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    const first = deferred<HostedToolsStatus>();
    const second = deferred<HostedToolsStatus>();
    const put = vi.spyOn(modelsApi, 'putHostedTool').mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let writes!: Promise<void>[];
    act(() => {
      writes = [
        result.current.save('web_search', { enabled: true }),
        result.current.save('x_search', { enabled: true }),
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

  it('MH-HOSTED-UI-007: drops a read that was already out when a write started', async () => {
    const first = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'getHostedTools').mockReturnValueOnce(first.promise);
    vi.spyOn(modelsApi, 'putHostedTool').mockResolvedValue(status(true));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });

    await act(async () => { await result.current.save('web_search', { enabled: true }); });
    expect(shown(result.current.read)).toBe(true);
    // The read cannot tell whether it saw the write, so its answer never lands.
    await act(async () => { first.resolve(status(false)); await first.promise; });
    expect(shown(result.current.read)).toBe(true);
  });

  it('MH-HOSTED-UI-007: re-reads after the newest write fails, because a lost answer may have committed', async () => {
    const get = vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    vi.spyOn(modelsApi, 'putHostedTool').mockRejectedValue(new Error('offline'));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));

    get.mockResolvedValue(status(true));
    await act(async () => { await result.current.save('web_search', { enabled: true }); });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(shown(result.current.read)).toBe(true);
  });

  it('MH-HOSTED-UI-007: re-reads when the open Source changes, since an edit can drop its evidence', async () => {
    const get = vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    const { rerender } = renderHook(({ revision }) => useHostedTools(true, revision), {
      wrapper,
      initialProps: { revision: 'source-a' },
    });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    rerender({ revision: 'source-a-with-a-new-key' });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it('MH-HOSTED-UI-007: shows no status of the previous Source identity while the new identity\'s read fails', async () => {
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValueOnce(status(true)).mockRejectedValue(new Error('offline'));
    const { result, rerender } = renderHook(({ revision }) => useHostedTools(true, revision), {
      wrapper,
      initialProps: { revision: 'identity-a' },
    });
    await waitFor(() => expect(shown(result.current.read)).toBe(true));

    // The key was replaced: the server dropped the old identity's evidence.
    rerender({ revision: 'identity-b' });
    await waitFor(() => expect(result.current.read.kind).not.toBe('loading'));
    expect(shown(result.current.read)).toBeNull();
  });

  it('MH-HOSTED-UI-007: keeps a tool pending until the read reconciling its lost write lands', async () => {
    const reconcile = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValueOnce(status(false)).mockReturnValueOnce(reconcile.promise);
    vi.spyOn(modelsApi, 'putHostedTool').mockRejectedValue(new Error('connection reset'));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let saving!: Promise<void>;
    act(() => { saving = result.current.save('web_search', { enabled: true }); });
    // The answer was lost and the write may have committed: still pending while the read is out.
    await waitFor(() => expect(modelsApi.getHostedTools).toHaveBeenCalledTimes(2));
    expect(result.current.pending.has('web_search')).toBe(true);

    await act(async () => { reconcile.resolve(status(true)); await saving; });
    expect(result.current.pending.has('web_search')).toBe(false);
    expect(shown(result.current.read)).toBe(true);
  });

  it('MH-HOSTED-UI-007: keeps a tool pending while the read reconciling its lost write fails, until a read lands', async () => {
    vi.spyOn(modelsApi, 'getHostedTools')
      .mockResolvedValueOnce(status(false))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(status(true));
    vi.spyOn(modelsApi, 'putHostedTool').mockRejectedValue(new Error('connection reset'));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    await act(async () => { await result.current.save('web_search', { enabled: true }); });
    // The write may have committed and the read that would say so failed: what
    // is shown is the pre-write status, so nothing may build on it.
    expect(modelsApi.getHostedTools).toHaveBeenCalledTimes(2);
    expect(result.current.read.kind).toBe('degraded');
    expect(result.current.pending.has('web_search')).toBe(true);

    // The tab's Retry reads again; that read lands and settles it.
    await act(async () => { await result.current.refresh(); });
    expect(shown(result.current.read)).toBe(true);
    expect(result.current.pending.has('web_search')).toBe(false);
  });

  it('MH-HOSTED-UI-007: holds every tool while the report is re-read, and after that read fails, until a read lands', async () => {
    const reread = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'getHostedTools')
      .mockResolvedValueOnce(status(false))
      .mockReturnValueOnce(reread.promise)
      .mockResolvedValueOnce(status(true));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    // An engine or Source change asks for a re-read: what is shown may no longer be what the server holds.
    let refreshing!: Promise<void>;
    act(() => { refreshing = result.current.refresh(); });
    expect(result.current.pending.has('web_search')).toBe(true);
    await act(async () => { reread.reject(new Error('offline')); await refreshing; });
    expect(result.current.read.kind).toBe('degraded');
    expect(result.current.pending.has('web_search')).toBe(true);

    await act(async () => { await result.current.refresh(); });
    expect(shown(result.current.read)).toBe(true);
    expect(result.current.pending.has('web_search')).toBe(false);
  });

  it.each([
    ['a refusal the server named', new ApiCallError('tool_unavailable', undefined, true), "Couldn't save the web search setting. Please retry."],
    ['a lost answer', new ApiCallError('http_502', undefined, false), "Couldn't confirm whether the web search setting was saved. It's being re-read from the server; check it before trying again."],
    ['an engine whose answer never reached the UI process', new ApiCallError('engine_down'), "Couldn't confirm whether the web search setting was saved. It's being re-read from the server; check it before trying again."],
    ['no answer at all', new TypeError('Failed to fetch'), "Couldn't confirm whether the web search setting was saved. It's being re-read from the server; check it before trying again."],
  ])('MH-HOSTED-UI-007: names the outcome of a save that ended in %s', async (_case, error, notice) => {
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    vi.spyOn(modelsApi, 'putHostedTool').mockRejectedValue(error);
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    await act(async () => { await result.current.save('web_search', { enabled: true }); });
    await screen.findByText(notice);
  });

  it('MH-HOSTED-UI-007: drops a write answer asked under another Source identity', async () => {
    const put = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValueOnce(status(false)).mockRejectedValue(new Error('offline'));
    vi.spyOn(modelsApi, 'putHostedTool').mockReturnValue(put.promise);
    const { result, rerender } = renderHook(({ revision }) => useHostedTools(true, revision), {
      wrapper,
      initialProps: { revision: 'identity-a' },
    });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let saving!: Promise<void>;
    act(() => { saving = result.current.save('web_search', { enabled: true }); });
    // The key is replaced while the write is out.
    rerender({ revision: 'identity-b' });
    await act(async () => { put.resolve(status(true)); await saving; });
    // That answer described the old identity; the new one's read failed, so nothing shows.
    expect(shown(result.current.read)).toBeNull();
  });

  it('MH-HOSTED-UI-007: keeps a tool pending until every write queued for it has settled', async () => {
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    const put = deferred<HostedToolsStatus>();
    const clear = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'putHostedTool').mockReturnValue(put.promise);
    vi.spyOn(modelsApi, 'clearHostedTool').mockReturnValue(clear.promise);
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let writes!: Promise<void>[];
    act(() => {
      writes = [
        result.current.save('web_search', { enabled: true }),
        result.current.clear('web_search', { source_id: 'src_relay001' }),
      ];
    });
    await act(async () => { put.resolve(status(true)); await writes[0]; });
    // The clear is still out and will answer a newer status: no control may build on this one.
    expect(result.current.pending.has('web_search')).toBe(true);

    await act(async () => { clear.resolve(status(true)); await writes[1]; });
    expect(result.current.pending.has('web_search')).toBe(false);
  });

  // Retry has no status payload: unlike PUT/clear it must keep the write queue
  // held through its follow-up GET, or an older read could overwrite a later save.
  it.each([
    ['web_search', 'src_exa000001'], ['web_search', 'src_exakeyless'],
    ['web_fetch', 'src_exakeyless'], ['web_fetch', 'src_jinakeyless'],
  ] as const)('serializes a %s retry and authoritative read before subsequent settings writes for %s', async (tool, sourceId) => {
    const refresh = deferred<HostedToolsStatus>();
    vi.spyOn(document, 'cookie', 'get').mockReturnValue('vibe_csrf_token=fixture-token');
    const get = vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValueOnce(status(false)).mockReturnValueOnce(refresh.promise);
    const fetch = vi.fn().mockResolvedValue(Response.json({ ok: true }));
    vi.stubGlobal('fetch', fetch);
    const put = vi.spyOn(modelsApi, 'putHostedTool').mockResolvedValue(status(false));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));
    let retrying!: Promise<void>;
    act(() => { retrying = tool === 'web_fetch' ? result.current.retryFetch(sourceId) : result.current.retryService(sourceId); });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(fetch).toHaveBeenCalledWith(`/api/models/hosted-tools/${tool}/services/${sourceId}/retry`, expect.objectContaining({ method: 'POST', body: '{}' }));
    expect(result.current.pending.has(tool)).toBe(true);
    let saving!: Promise<void>;
    act(() => { saving = result.current.save(tool, { services: { [sourceId]: { enabled: false } } }); });
    expect(put).not.toHaveBeenCalled();
    await act(async () => { refresh.resolve(status(true)); await retrying; await saving; });
    expect(put).toHaveBeenCalledWith(tool, { services: { [sourceId]: { enabled: false } } });
    expect(shown(result.current.read)).toBe(false);
    expect(result.current.pending.has(tool)).toBe(false);
  });

  it('MH-HOSTED-UI-007: queues a clear behind a write like any write, so its answer lands last', async () => {
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValue(status(false));
    const put = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'putHostedTool').mockReturnValue(put.promise);
    const clear = vi.spyOn(modelsApi, 'clearHostedTool').mockResolvedValue(status(false));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let writes!: Promise<void>[];
    act(() => {
      writes = [
        result.current.save('web_search', { enabled: true }),
        result.current.clear('web_search', { source_id: 'src_relay001', model_id: 'gpt-6-astra' }),
      ];
    });
    await waitFor(() => expect(modelsApi.putHostedTool).toHaveBeenCalledTimes(1));
    expect(clear).not.toHaveBeenCalled();
    await act(async () => { put.resolve(status(true)); await Promise.all(writes); });
    expect(clear).toHaveBeenCalledWith('web_search', { source_id: 'src_relay001', model_id: 'gpt-6-astra' });
    expect(shown(result.current.read)).toBe(false);
  });
});
