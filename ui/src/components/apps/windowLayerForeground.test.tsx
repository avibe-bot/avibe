/** @vitest-environment jsdom */

// A window body is a retained surface too. It keeps running while Settings
// covers the layer — `AppsPreviewPage` awaits `fileMeta()` before opening the
// editor, `AppsFileBrowserPage` awaits a content hit the same way — so an await
// begun before the overlay resolves after it, and the `openApp` that follows
// would take Settings down on behalf of a user who has moved on.
//
// Nothing in the body knows that. What makes it safe is where the body is
// rendered: this layer wraps its windows in the same activity boundary a
// retained route gets, fed by the same `active` the shell hides the layer with.
// `useWindowManager` reads that boundary, so the body inherits the answer
// without asking. This pins the wiring end to end — the layer's own prop
// through the boundary into the announcement — because reading either half
// alone invites the conclusion that window bodies are ungated.

import { createInstance } from 'i18next';
import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { useWindowManager, type WindowManagerValue } from '../../context/WindowManagerContext';
import { WindowManagerProvider } from '../../context/WindowManagerProvider';
import { WindowLayer } from './WindowLayer';

const dockState = { order: [] as string[], pins: [] as unknown[] };
const api = vi.hoisted(() => ({
  getShowPages: vi.fn(),
  getSessionResult: vi.fn(),
  connectWorkbenchEvents: vi.fn(),
}));

vi.mock('../../context/DockContext', () => ({ useDock: () => dockState }));
vi.mock('../../context/StandaloneAppTabContext', () => ({ useStandaloneAppTab: () => false }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../useShowPages', () => ({ useShowPageInventory: () => ({ pages: [] }) }));
vi.mock('../workbench/ShowPageAnnotationHost', () => ({
  ShowPageAnnotationHost: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// Stands in for a real app body — the only thing that matters here is that it
// takes the manager the way one does and hands the reference back out, which is
// what an awaited continuation holds on to.
const body = { manager: null as WindowManagerValue | null };
vi.mock('./AppWindow', () => ({
  AppWindow: () => {
    const value = useWindowManager();
    useEffect(() => {
      body.manager = value;
    }, [value]);
    return <div data-window-id="win_1" />;
  },
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

describe('window bodies inherit the layer retirement', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    dockState.order = [];
    dockState.pins = [];
    api.getShowPages.mockReset().mockResolvedValue([]);
    api.getSessionResult.mockReset().mockResolvedValue({ status: null, session: null });
    api.connectWorkbenchEvents.mockReset().mockReturnValue(vi.fn());
    body.manager = null;
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  const renderShell = (onWindowForeground: () => void) => {
    let shell: WindowManagerValue | null = null;
    const Shell = ({ active }: { active: boolean }) => {
      // The shell reads the manager from ABOVE the layer, the way AppShell does.
      const value = useWindowManager();
      useEffect(() => {
        shell = value;
      }, [value]);
      return <WindowLayer active={active} />;
    };
    const view = render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <WindowManagerProvider onWindowForeground={onWindowForeground}>
            <Shell active />
          </WindowManagerProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    return {
      shell: () => shell!,
      setActive: (active: boolean) => view.rerender(
        <MemoryRouter>
          <I18nextProvider i18n={i18n}>
            <WindowManagerProvider onWindowForeground={onWindowForeground}>
              <Shell active={active} />
            </WindowManagerProvider>
          </I18nextProvider>
        </MemoryRouter>,
      ),
    };
  };

  it('withholds the announcement from an await that resolves after Settings opens', () => {
    const onForeground = vi.fn();
    const { shell, setActive } = renderShell(onForeground);

    // A window exists, so a body is mounted and has taken its manager.
    act(() => void shell().openApp('preview'));
    expect(body.manager).not.toBeNull();
    // What the body captured before the await — `AppsPreviewPage` holds exactly
    // this across `await fileMeta()`.
    const openInEditor = body.manager!.openApp;
    onForeground.mockClear();

    // Settings opens: the shell hides the layer, and that is the whole signal.
    act(() => setActive(false));
    act(() => void openInEditor('editor'));

    // The editor opened and is on top of the window stack — it is simply waiting
    // behind Settings rather than throwing it away.
    expect(onForeground).not.toHaveBeenCalled();
    expect(shell().windows.some((win) => win.appId === 'editor')).toBe(true);
  });

  it('still announces once the layer is the live surface again', () => {
    const onForeground = vi.fn();
    const { shell, setActive } = renderShell(onForeground);

    act(() => void shell().openApp('preview'));
    const openInEditor = body.manager!.openApp;
    onForeground.mockClear(); // that open was the shell's own, and did announce
    act(() => setActive(false));
    act(() => void openInEditor('editor'));
    expect(onForeground).not.toHaveBeenCalled();

    // Same reference, live layer — so the test above is about who is asking at
    // the moment of the call, not a stale closure that stopped working.
    act(() => setActive(true));
    act(() => void openInEditor('editor'));
    expect(onForeground).toHaveBeenCalledTimes(1);
  });
});
