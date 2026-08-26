/** @vitest-environment jsdom */

// The window layer is mounted shell-wide so a window survives navigation, which
// made it the last unconditional reader of /api/show-pages: every document paid
// for the inventory, including the ones that never open a window. Same class as
// the demand-driven providers in ``src/context/shellBootstrapDemand.test.tsx`` —
// what the document renders decides what is fetched.

import { createInstance } from 'i18next';
import { act, cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import type { ShowPage } from '../../lib/showPagesStore';
import type { WindowInstance } from '../../context/WindowManagerContext';
import { WindowLayer } from './WindowLayer';

const windowsRef = { current: [] as WindowInstance[] };
const windowManager = vi.hoisted(() => ({
  close: vi.fn(),
  focus: vi.fn(),
  minimize: vi.fn(),
  openApp: vi.fn(),
  restore: vi.fn(),
  setParams: vi.fn(),
  setTitle: vi.fn(),
  confirmClose: vi.fn(() => true),
}));

vi.mock('../../context/DockContext', () => ({
  useDock: () => ({ order: [], pins: [] }),
}));

vi.mock('../../context/WindowManagerContext', () => ({
  useWindowManager: () => ({
    windows: windowsRef.current,
    ...windowManager,
  }),
}));

vi.mock('../../context/StandaloneAppTabContext', () => ({
  useStandaloneAppTab: () => false,
}));

// Every window body is an app of its own (iframe, terminal, editor); the layer's
// own fetch decision is what is under test, so the frame is stubbed out.
vi.mock('./AppWindow', () => ({
  AppWindow: () => <button type="button" data-testid="app-window" data-window-id="win_1" />,
}));
vi.mock('../workbench/ShowPageAnnotationHost', () => ({
  ShowPageAnnotationHost: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const api = vi.hoisted(() => ({
  getShowPages: vi.fn(),
  getSessionResult: vi.fn(),
  connectWorkbenchEvents: vi.fn(),
}));

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const appWindow = (): WindowInstance =>
  ({
    id: 'win_1',
    appId: 'files',
    title: 'Files',
    x: 0,
    y: 0,
    w: 800,
    h: 600,
    z: 1,
    minimized: false,
    maximized: false,
    params: {},
  }) as unknown as WindowInstance;

const renderLayer = () =>
  render(
    <I18nextProvider i18n={i18n}>
      <WindowLayer />
    </I18nextProvider>,
  );

describe('WindowLayer show-pages inventory', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    windowsRef.current = [];
    api.getShowPages.mockReset();
    api.getShowPages.mockResolvedValue([] as ShowPage[]);
    api.getSessionResult.mockReset();
    api.getSessionResult.mockResolvedValue({ status: null, session: null });
    api.connectWorkbenchEvents.mockReset();
    api.connectWorkbenchEvents.mockReturnValue(vi.fn());
    Object.values(windowManager).forEach((mock) => mock.mockClear());
  });

  afterEach(() => {
    cleanup();
  });

  it('leaves the inventory unfetched while no window is open', async () => {
    renderLayer();
    await vi.waitFor(() => expect(api.connectWorkbenchEvents).toHaveBeenCalled());

    expect(api.getShowPages).not.toHaveBeenCalled();
  });

  it('reads the inventory once a window exists', async () => {
    windowsRef.current = [appWindow()];
    renderLayer();

    await vi.waitFor(() => expect(api.getShowPages).toHaveBeenCalledTimes(1));
  });

  it('yields window chords already consumed by the focused surface', async () => {
    windowsRef.current = [appWindow()];
    renderLayer();
    const windowRoot = await screen.findByTestId('app-window');
    windowRoot.focus();

    const consumed = new KeyboardEvent('keydown', {
      key: 'm',
      code: 'KeyM',
      metaKey: true,
      bubbles: true,
      cancelable: true,
    });
    consumed.preventDefault();
    act(() => window.dispatchEvent(consumed));

    expect(windowManager.minimize).not.toHaveBeenCalled();
  });
});
