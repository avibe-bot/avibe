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

  it('keeps the status a probe answered with when the read after it fails', async () => {
    // Before: unchecked (shown here as off). The probe's answer: the tool's state after it.
    vi.spyOn(modelsApi, 'getHostedTools')
      .mockResolvedValueOnce(status(false))
      .mockRejectedValueOnce(new Error('offline'));
    vi.spyOn(modelsApi, 'probeHostedTool').mockResolvedValue({
      probe: { tool: 'web_search', source_id: 'src_relay001', model_id: 'gpt-6-astra', call: { outcome: 'verified', error: null }, replay: null },
      hosted_tools: status(true),
    });
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    const candidate = {
      source_id: 'src_relay001', model_id: 'gpt-6-astra', billing: 'metered' as const, price: null, probe_estimate: null,
      call: { status: 'unknown' as const, stale: null, basis: null, observed_at: null, detail: null, changes_at: null },
      replay: null,
    };
    await act(async () => { await result.current.probe({ tool: 'web_search', candidate }); });
    // The read that follows failed; what the probe answered still stands.
    await waitFor(() => expect(shown(result.current.read)).toBe(true));
  });

  it('reads the record before calling a probe unanswered when its answer was lost', async () => {
    const before = { status: 'unknown' as const, stale: null, basis: null, observed_at: null, detail: null, changes_at: null };
    const candidate = {
      source_id: 'src_relay001', model_id: 'gpt-6-astra', billing: 'metered' as const, price: null, probe_estimate: null, call: before, replay: null,
    };
    const recorded: HostedToolsStatus = {
      tools: [{
        name: 'web_search', enabled: false, executor: null, bound: null,
        candidates: [{ ...candidate, call: { ...before, status: 'verified', basis: 'probe', observed_at: '2026-10-14T07:00:00Z' } }],
      }],
      reverify: { engine_upgraded: 0, aged: 0 },
    };
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValueOnce(status(false)).mockResolvedValue(recorded);
    // The probe ran and recorded its evidence, but its answer never arrived.
    vi.spyOn(modelsApi, 'probeHostedTool').mockRejectedValue(new Error('connection reset'));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let outcome: Awaited<ReturnType<typeof result.current.probe>> = null;
    await act(async () => { outcome = await result.current.probe({ tool: 'web_search', candidate }); });
    // Verified, not "no answer", so nothing invites a second paid request.
    expect(outcome).toMatchObject({ call: { outcome: 'verified' } });
  });

  it('lands the status read for a lost probe answer, even when the read after it fails', async () => {
    const before = { status: 'unknown' as const, stale: null, basis: null, observed_at: null, detail: null, changes_at: null };
    const candidate = {
      source_id: 'src_relay001', model_id: 'gpt-6-astra', billing: 'metered' as const, price: null, probe_estimate: null, call: before, replay: null,
    };
    const recorded: HostedToolsStatus = {
      tools: [{
        name: 'web_search', enabled: true, executor: null, bound: null,
        candidates: [{ ...candidate, call: { ...before, status: 'verified', basis: 'probe', observed_at: '2026-10-14T07:00:00Z' } }],
      }],
      reverify: { engine_upgraded: 0, aged: 0 },
    };
    vi.spyOn(modelsApi, 'getHostedTools')
      .mockResolvedValueOnce(status(false))
      .mockResolvedValueOnce(recorded)
      .mockRejectedValue(new Error('offline'));
    vi.spyOn(modelsApi, 'probeHostedTool').mockRejectedValue(new Error('connection reset'));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    await act(async () => { await result.current.probe({ tool: 'web_search', candidate }); });
    // The recovered status shows; the failed read after it only marks it degraded.
    await waitFor(() => expect(shown(result.current.read)).toBe(true));
  });

  it('shows no status of the previous Source identity while the new identity\'s read fails', async () => {
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

  it('keeps a tool pending until the read reconciling its lost write lands', async () => {
    const reconcile = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValueOnce(status(false)).mockReturnValueOnce(reconcile.promise);
    vi.spyOn(modelsApi, 'putHostedTool').mockRejectedValue(new Error('connection reset'));
    const { result } = renderHook(() => useHostedTools(true), { wrapper });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let saving!: Promise<void>;
    act(() => { saving = result.current.save('web_search', { enabled: true, executor: null }); });
    // The answer was lost and the write may have committed: still pending while the read is out.
    await waitFor(() => expect(modelsApi.getHostedTools).toHaveBeenCalledTimes(2));
    expect(result.current.pending.has('web_search')).toBe(true);

    await act(async () => { reconcile.resolve(status(true)); await saving; });
    expect(result.current.pending.has('web_search')).toBe(false);
    expect(shown(result.current.read)).toBe(true);
  });

  it('drops a write answer asked under another Source identity', async () => {
    const put = deferred<HostedToolsStatus>();
    vi.spyOn(modelsApi, 'getHostedTools').mockResolvedValueOnce(status(false)).mockRejectedValue(new Error('offline'));
    vi.spyOn(modelsApi, 'putHostedTool').mockReturnValue(put.promise);
    const { result, rerender } = renderHook(({ revision }) => useHostedTools(true, revision), {
      wrapper,
      initialProps: { revision: 'identity-a' },
    });
    await waitFor(() => expect(shown(result.current.read)).toBe(false));

    let saving!: Promise<void>;
    act(() => { saving = result.current.save('web_search', { enabled: true, executor: null }); });
    // The key is replaced while the write is out.
    rerender({ revision: 'identity-b' });
    await act(async () => { put.resolve(status(true)); await saving; });
    // That answer described the old identity; the new one's read failed, so nothing shows.
    expect(shown(result.current.read)).toBeNull();
  });
});
