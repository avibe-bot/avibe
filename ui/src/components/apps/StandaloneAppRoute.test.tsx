/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AppLaunchSnapshot } from '../../apps/appLaunchProtocol';

type Activity = { event: string; session_id: string; title?: string | null };
type Handlers = {
  onConnected?: () => void;
  onSessionActivity?: (data: Activity) => void;
};

const state = vi.hoisted(() => ({ snapshot: null as AppLaunchSnapshot | null }));
const handlers = new Set<Handlers>();
const getSessionResult = vi.fn(async (id: string): Promise<{ status: number; session: { id: string; status: string; title?: string | null } | null }> => ({
  status: 200,
  session: { id, status: 'active' },
}));
const api = {
  getSessionResult,
  connectWorkbenchEvents: (next: Handlers) => {
    handlers.add(next);
    return () => handlers.delete(next);
  },
};

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../context/StandaloneAppTabContext', () => ({ useStandaloneAppTab: () => true }));
vi.mock('../../apps/useAppLaunchSnapshot', () => ({ useAppLaunchSnapshot: () => state.snapshot }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => (key === 'chat.untitled' ? 'Untitled session' : key) }) }));
vi.mock('./NativeShowShareEntry', () => ({ NativeShowShareEntry: () => null }));
vi.mock('../workbench/AppsEditorPage', () => ({ AppsEditorPage: () => null }));
vi.mock('../../apps/registry', async () => {
  const { useContext } = await import('react');
  const { WindowManagerContext } = await import('../../context/WindowManagerContext');
  // The Show body closes its own window the way ShowPageApp does.
  const Component = ({ windowId }: { windowId: string }) => {
    const manager = useContext(WindowManagerContext);
    return <>
      <button onClick={() => { if (manager?.confirmClose(windowId)) manager.close(windowId); }}>close own</button>
      <button onClick={() => { if (manager?.confirmClose('other')) manager.close('other'); }}>close other</button>
    </>;
  };
  return { APP_REGISTRY: { showpage: { Component } } };
});
vi.mock('../workbench/ShowPageAnnotationHost', () => ({
  ShowPageAnnotationHost: ({ src, children }: { src: string | null; children: ReactNode }) => (
    <div data-testid="show-host" data-src={src ?? ''}>{children}</div>
  ),
}));

import { StandaloneAppRoute } from './StandaloneAppRoute';
import { WindowManagerContext, type WindowManagerValue } from '../../context/WindowManagerContext';

function renderShow(sessionId: string, manager: WindowManagerValue | null = null) {
  render(
    <WindowManagerContext.Provider value={manager}>
      <MemoryRouter initialEntries={[`/apps/show/${sessionId}`]}>
        <Routes>
          <Route path="/apps/show/:sessionId" element={<StandaloneAppRoute appId="showpage" />} />
        </Routes>
      </MemoryRouter>
    </WindowManagerContext.Provider>,
  );
  return () => screen.getByTestId('show-host').getAttribute('data-src');
}

const emit = (event: Activity) =>
  act(() => handlers.forEach((handler) => handler.onSessionActivity?.(event)));
const reconnect = () => act(() => handlers.forEach((handler) => handler.onConnected?.()));

beforeEach(() => {
  state.snapshot = null;
  document.title = 'Avibe';
});

afterEach(() => {
  cleanup();
  handlers.clear();
  getSessionResult.mockClear();
});

describe('standalone Show window lifecycle', () => {
  it('withdraws the page when its session is archived, ignoring other sessions', async () => {
    const src = renderShow('ses-live');
    await waitFor(() => expect(src()).not.toBe(''));
    emit({ event: 'archived', session_id: 'ses-other' });
    expect(src()).not.toBe('');
    emit({ event: 'archived', session_id: 'ses-live' });
    expect(src()).toBe('');
  });

  it('re-reads the session on every event-stream (re)connect', async () => {
    const src = renderShow('ses-reconnect');
    await waitFor(() => expect(src()).not.toBe(''));
    expect(getSessionResult).toHaveBeenCalledTimes(1);
    getSessionResult.mockResolvedValueOnce({ status: 200, session: { id: 'ses-reconnect', status: 'archived' } });
    act(() => handlers.forEach((handler) => handler.onConnected?.()));
    await waitFor(() => expect(src()).toBe(''));
    expect(getSessionResult).toHaveBeenCalledTimes(2);
  });
});

describe('closing a standalone body', () => {
  const manager = () => ({ close: vi.fn(), confirmClose: vi.fn(() => true) }) as unknown as WindowManagerValue & {
    close: ReturnType<typeof vi.fn>;
    confirmClose: ReturnType<typeof vi.fn>;
  };
  const nativeCloses: string[] = [];
  const intercept = (event: MouseEvent) => {
    const link = event.target as HTMLAnchorElement;
    if (!link.href?.startsWith('avibe-app:')) return;
    event.preventDefault();
    nativeCloses.push(link.href);
  };
  afterEach(() => {
    document.removeEventListener('click', intercept);
    nativeCloses.length = 0;
    Reflect.deleteProperty(window, '__AVIBE_NATIVE_APP_WINDOW__');
    delete window.__AVIBE_APP_LAUNCH_PROTOCOL__;
  });

  it('sends its own window to the native close and other windows to the manager', () => {
    Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true, configurable: true });
    window.__AVIBE_APP_LAUNCH_PROTOCOL__ = 1;
    document.addEventListener('click', intercept);
    const wm = manager();
    renderShow('ses-own', wm);
    fireEvent.click(screen.getByRole('button', { name: 'close own' }));
    expect(nativeCloses).toEqual(['avibe-app://close']);
    expect(wm.confirmClose).not.toHaveBeenCalled();
    expect(wm.close).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'close other' }));
    expect(wm.close).toHaveBeenCalledWith('other');
    expect(nativeCloses).toHaveLength(1);
  });

  it('keeps the in-page manager outside a native App window', () => {
    document.addEventListener('click', intercept);
    const wm = manager();
    renderShow('ses-tab', wm);
    fireEvent.click(screen.getByRole('button', { name: 'close own' }));
    expect(wm.close).toHaveBeenCalledWith('route-showpage');
    expect(nativeCloses).toEqual([]);
  });
});

describe('a native Show window names itself after its Show', () => {
  beforeEach(() => {
    Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true, configurable: true });
  });
  afterEach(() => {
    Reflect.deleteProperty(window, '__AVIBE_NATIVE_APP_WINDOW__');
  });

  it('takes the Show title when opened from the route or a deep link, which name nothing', async () => {
    getSessionResult.mockResolvedValueOnce({ status: 200, session: { id: 'ses-1', status: 'active', title: 'Read name' } });
    renderShow('ses-1');
    await waitFor(() => expect(document.title).toBe('Read name'));
  });

  it('follows a rename, ignoring other sessions, and takes a later re-read over an earlier rename', async () => {
    getSessionResult.mockResolvedValueOnce({ status: 200, session: { id: 'ses-1', status: 'active', title: 'Q4 漏斗看板' } });
    renderShow('ses-1');
    await waitFor(() => expect(document.title).toBe('Q4 漏斗看板'));
    emit({ event: 'updated', session_id: 'ses-other', title: 'Other' });
    emit({ event: 'updated', session_id: 'ses-1', title: 'Q4 漏斗看板（终版）' });
    await waitFor(() => expect(document.title).toBe('Q4 漏斗看板（终版）'));
    // A rename missed while disconnected arrives with the re-read.
    getSessionResult.mockResolvedValueOnce({ status: 200, session: { id: 'ses-1', status: 'active', title: 'Q4 定稿' } });
    reconnect();
    await waitFor(() => expect(document.title).toBe('Q4 定稿'));
  });

  it('uses the launcher title until the Show is read, and untitled for a Show without one', async () => {
    state.snapshot = { appId: 'showpage', sessionId: 'ses-1', title: 'Launcher name', requestId: 'r1', windowId: 'w1', source: 'dock' };
    getSessionResult.mockResolvedValueOnce({ status: 500, session: null });
    renderShow('ses-1');
    await waitFor(() => expect(document.title).toBe('Launcher name'));
    emit({ event: 'updated', session_id: 'ses-1', title: null });
    await waitFor(() => expect(document.title).toBe('Untitled session'));
  });

  it('leaves a browser tab title alone', async () => {
    Reflect.deleteProperty(window, '__AVIBE_NATIVE_APP_WINDOW__');
    getSessionResult.mockResolvedValueOnce({ status: 200, session: { id: 'ses-1', status: 'active', title: 'Q4 漏斗看板' } });
    const src = renderShow('ses-1');
    await waitFor(() => expect(src()).not.toBe(''));
    expect(document.title).toBe('Avibe');
  });
});
