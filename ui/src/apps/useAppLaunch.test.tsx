/** @vitest-environment jsdom */
import { act, cleanup, render, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { useEffect, type ReactNode } from 'react';
import { useAppLaunch } from './useAppLaunch';
import { useAppLaunchSnapshot } from './useAppLaunchSnapshot';
import { APP_LAUNCH_RESULT_EVENT, validAppLaunchTarget, type AppLaunchRequest, type AppLaunchSnapshot, type AppLaunchTarget } from './appLaunchProtocol';
import { WindowManagerProvider } from '../context/WindowManagerProvider';
import { useWindowManager } from '../context/WindowManagerContext';
import { RouteSurfaceActiveContext } from '../lib/routeSurfaceActivity';

vi.mock('../lib/useIsDesktop', () => ({ isDesktopViewport: () => true }));
let requests: AppLaunchRequest[] = [];
let disposition: 'opened' | 'fallback' | 'stale' | null = 'opened';
const intercept = (event: MouseEvent) => {
  const anchor = event.target as HTMLAnchorElement;
  if (!anchor.href?.startsWith('avibe-app://launch?')) return;
  event.preventDefault();
  const request = JSON.parse(new URL(anchor.href).searchParams.get('request')!) as AppLaunchRequest;
  requests.push(request);
  if (disposition) window.dispatchEvent(new CustomEvent(APP_LAUNCH_RESULT_EVENT, { detail: { requestId: request.requestId, status: disposition } }));
};
const Wrapper = ({ children, active = true }: { children: ReactNode; active?: boolean }) => <MemoryRouter><WindowManagerProvider standalone><RouteSurfaceActiveContext.Provider value={active}>{children}</RouteSurfaceActiveContext.Provider></WindowManagerProvider></MemoryRouter>;
beforeEach(() => {
  requests = [];
  disposition = 'opened';
  window.__AVIBE_APP_LAUNCH_PROTOCOL__ = 1;
  document.addEventListener('click', intercept);
});
afterEach(() => { cleanup(); document.removeEventListener('click', intercept); delete window.__AVIBE_APP_LAUNCH_PROTOCOL__; delete window.__AVIBE_APP_LAUNCH__; Reflect.deleteProperty(window, '__AVIBE_NATIVE_APP_WINDOW__'); vi.useRealTimers(); vi.restoreAllMocks(); localStorage.clear(); });
const targets: AppLaunchTarget[] = [
  { appId: 'files', params: { path: '/fixture/files' } },
  { appId: 'terminal', params: { cwd: '/fixture/cwd' } },
  { appId: 'editor', params: { path: '/fixture/a.ts', filename: 'a.ts', mtime: 2, line: 3, column: 0, endColumn: 1 } },
  { appId: 'preview', params: { path: '/fixture/a.png', name: 'a.png' } },
  { appId: 'showpage', sessionId: 'legacy:show.1', title: 'Test' },
  { appId: 'library', params: { initialTab: 'showpages' } },
];

describe('W1 resolver owns launch intent, not native authority', () => {
  it.each(targets)('transmits complete $appId state and listens before synchronous result', async (target) => {
    const { result } = renderHook(() => ({ launch: useAppLaunch('file-browser'), wm: useWindowManager() }), { wrapper: Wrapper });
    await act(async () => expect(await result.current.launch(target, 'newWindow')).toBe('opened'));
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({ ...target, mode: 'newWindow', newWindow: true, source: 'file-browser', origin: location.origin });
    expect(result.current.wm.windows).toHaveLength(0);
  });
  it('uses explicit fallback once and activates the existing window with fresh state', async () => {
    disposition = 'fallback';
    const { result } = renderHook(() => ({ launch: useAppLaunch('file-browser'), wm: useWindowManager() }), { wrapper: Wrapper });
    await act(async () => { await result.current.launch(targets[2]); });
    const id = result.current.wm.windows[0].id;
    act(() => window.dispatchEvent(new CustomEvent(APP_LAUNCH_RESULT_EVENT, { detail: { requestId: requests[0].requestId, status: 'fallback' } })));
    await act(async () => { await result.current.launch({ appId: 'editor', params: { newFileDir: '/draft' } }); });
    expect(result.current.wm.windows).toHaveLength(1);
    expect(result.current.wm.windows[0]).toMatchObject({ id, params: { newFileDir: '/draft', path: undefined } });
  });
  it('returns DOM focus to an already-focused window from the launcher', async () => {
    disposition = 'fallback';
    const { result } = renderHook(() => ({ launch: useAppLaunch('dock'), wm: useWindowManager() }), { wrapper: Wrapper });
    await act(async () => { await result.current.launch({ appId: 'files' }); });
    const id = result.current.wm.windows[0].id;
    expect(result.current.wm.focusedId).toBe(id);
    const root = document.createElement('div');
    root.tabIndex = -1;
    root.dataset.windowId = id;
    const field = document.createElement('input');
    root.append(field);
    const launcher = document.createElement('button');
    document.body.append(root, launcher);
    try {
      launcher.focus();
      await act(async () => { await result.current.launch({ appId: 'files' }); });
      expect(result.current.wm.windows).toHaveLength(1);
      expect(document.activeElement).toBe(root);
      field.focus();
      await act(async () => { await result.current.launch({ appId: 'files' }); });
      expect(document.activeElement).toBe(field);
    } finally {
      root.remove();
      launcher.remove();
    }
  });
  it('old shells cannot silently drop rich params through the legacy invoke', async () => {
    delete window.__AVIBE_APP_LAUNCH_PROTOCOL__;
    const { result } = renderHook(() => ({ launch: useAppLaunch('file-browser'), wm: useWindowManager() }), { wrapper: Wrapper });
    await act(async () => { await result.current.launch(targets[2], 'newTab'); });
    expect(requests).toEqual([]);
    expect(result.current.wm.windows[0].params).toMatchObject(targets[2].params!);
  });
  it('does not duplicate an unknown timed-out dispatch or replay late results', async () => {
    vi.useFakeTimers(); disposition = null;
    const { result } = renderHook(() => ({ launch: useAppLaunch('dock'), wm: useWindowManager() }), { wrapper: Wrapper });
    let pending: ReturnType<typeof result.current.launch>;
    act(() => { pending = result.current.launch({ appId: 'files' }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); expect(await pending!).toBe('unknown'); });
    act(() => window.dispatchEvent(new CustomEvent(APP_LAUNCH_RESULT_EVENT, { detail: { requestId: requests[0].requestId, status: 'fallback' } })));
    expect(result.current.wm.windows).toEqual([]);
  });
  it('retired route commands settle locally without asking native to take focus', async () => {
    const { result } = renderHook(() => ({ launch: useAppLaunch('show-route'), wm: useWindowManager() }), { wrapper: ({ children }) => <Wrapper active={false}>{children}</Wrapper> });
    await act(async () => { await result.current.launch(targets[4]); });
    expect(requests).toEqual([]);
    expect(result.current.wm.windows).toHaveLength(1);
  });
  it('aborts an unmounted source and ignores fallback in a successor', async () => {
    disposition = null;
    const { result, unmount } = renderHook(() => useAppLaunch('library'), { wrapper: Wrapper });
    let pending: ReturnType<typeof result.current>;
    act(() => { pending = result.current({ appId: 'files' }); });
    unmount();
    expect(await pending!).toBe('stale');
    window.dispatchEvent(new CustomEvent(APP_LAUNCH_RESULT_EVENT, { detail: { requestId: requests[0].requestId, status: 'fallback' } }));
  });
  it('opens identity-only tabs synchronously and normalizes rich tabs to activate', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const { result } = renderHook(() => useAppLaunch('dock'), { wrapper: Wrapper });
    let pending: ReturnType<typeof result.current>;
    act(() => { pending = result.current({ appId: 'files' }, 'newTab'); expect(open).toHaveBeenCalledOnce(); });
    await pending!;
    await act(async () => { await result.current(targets[2], 'newTab'); });
    expect(open).toHaveBeenCalledOnce();
    expect(requests[0]).toMatchObject({ mode: 'activate', params: targets[2].params });
  });
});

it('validates closed target fields, UTF8 limits and zero-based columns', () => {
  expect(validAppLaunchTarget(targets[2])).toBe(true);
  for (const target of [
    { appId: 'terminal', params: { path: '/wrong' } }, { appId: 'editor', params: { content: 'secret' } },
    { appId: 'editor', params: { column: -1 } }, { appId: 'editor', params: { column: 2, endColumn: 1 } },
    { appId: 'editor', params: { mtime: Infinity } }, { appId: 'files', params: { path: '中'.repeat(1366) } },
    { appId: 'showpage', sessionId: '../oops' },
    ...['_show', '.show', ':show'].map((sessionId) => ({ appId: 'showpage', sessionId })),
  ]) expect(validAppLaunchTarget(target)).toBe(false);
});

it('reads native initial/repeated snapshots and rejects duplicate or foreign window ownership', async () => {
  Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true, configurable: true });
  const initial: AppLaunchSnapshot = { appId: 'editor', windowId: 'native-editor', requestId: 'one', source: 'file-browser', params: { path: '/one' } };
  window.__AVIBE_APP_LAUNCH__ = initial;
  const { result } = renderHook(() => useAppLaunchSnapshot());
  expect(result.current).toEqual(initial);
  const send = (snapshot: AppLaunchSnapshot) => act(() => window.dispatchEvent(new CustomEvent('avibe:app-launch', { detail: snapshot })));
  send({ ...initial, requestId: 'two', params: { newFileDir: '/draft' } });
  await waitFor(() => expect(result.current?.params).toEqual({ newFileDir: '/draft' }));
  send(initial);
  send({ ...initial, requestId: 'three', windowId: 'other' });
  expect(result.current?.requestId).toBe('two');
});

it('settles an already-submitted launch after its transient launcher closes, but rejects later stale calls', async () => {
  disposition = null;
  let launch: ReturnType<typeof useAppLaunch> | null = null;
  const Consumer = () => { const value = useAppLaunch('command-palette'); useEffect(() => { launch = value; }, [value]); return null; };
  let manager: ReturnType<typeof useWindowManager> | null = null;
  const Probe = () => { const value = useWindowManager(); useEffect(() => { manager = value; }, [value]); return null; };
  const { rerender } = render(<Wrapper><Consumer /><Probe /></Wrapper>);
  let pending: ReturnType<ReturnType<typeof useAppLaunch>>;
  act(() => { pending = launch!({ appId: 'files' }); });
  rerender(<Wrapper><Probe /></Wrapper>);
  await act(async () => {
    window.dispatchEvent(new CustomEvent(APP_LAUNCH_RESULT_EVENT, { detail: { requestId: requests[0].requestId, status: 'fallback' } }));
    expect(await pending!).toBe('fallback');
  });
  expect(manager!.windows).toHaveLength(1);
  expect(await launch!({ appId: 'editor' })).toBe('stale');
  expect(requests).toHaveLength(1);
});

it('serial fallback activation before React commit reuses one manager-owned App window', async () => {
  delete window.__AVIBE_APP_LAUNCH_PROTOCOL__;
  const { result } = renderHook(() => ({ launch: useAppLaunch('dock'), wm: useWindowManager() }), { wrapper: Wrapper });
  await act(async () => { await Promise.all([result.current.launch({ appId: 'files' }), result.current.launch({ appId: 'files' })]); });
  expect(result.current.wm.windows).toHaveLength(1);
});

describe('launch lifetime and request identity', () => {
  it('launches without crypto.randomUUID, as on a plain-HTTP LAN origin', async () => {
    // An own property shadows the prototype's method only for this test; deleting it restores it.
    Object.defineProperty(globalThis.crypto, 'randomUUID', { value: undefined, configurable: true });
    try {
      const { result } = renderHook(() => useAppLaunch('file-browser'), { wrapper: Wrapper });
      await act(async () => expect(await result.current(targets[0], 'newWindow')).toBe('opened'));
      expect(requests[0].requestId).toMatch(/^[0-9a-f]{32}$/);
    } finally {
      Reflect.deleteProperty(globalThis.crypto, 'randomUUID');
    }
    expect(typeof globalThis.crypto.randomUUID).toBe('function');
  });
  it('renews the owner lifetime after a back-forward cache restore', async () => {
    const { result } = renderHook(() => useAppLaunch('file-browser'), { wrapper: Wrapper });
    act(() => { window.dispatchEvent(new Event('pagehide')); });
    await act(async () => expect(await result.current(targets[0], 'newWindow')).toBe('stale'));
    act(() => { window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })); });
    await act(async () => expect(await result.current(targets[0], 'newWindow')).toBe('opened'));
  });
});
