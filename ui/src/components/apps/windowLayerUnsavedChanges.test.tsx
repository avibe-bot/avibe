/** @vitest-environment jsdom */

// Windows outlive every route change: opening Settings hides the layer, closing
// it shows the layer again, and a route navigation leaves the layer mounted. So
// no navigation can take a window's work, and the route blocker must never ask
// on a window's behalf. The window's close guard owns its dirty state. This pins
// that at the layer, where it is decided, rather than leaving it to whether some
// window body happens to register a route claim.

import { createInstance } from 'i18next';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect } from 'react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import {
  Link,
  RouterProvider,
  createMemoryRouter,
  useLocation,
  useNavigate,
} from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { UnsavedChangesProvider } from '../../context/UnsavedChangesProvider';
import { useUnsavedChanges } from '../../context/useUnsavedChanges';
import { useUnsavedChangesActionGuard } from '../../context/useUnsavedChangesActionGuard';
import { useWindowManager, type WindowManagerValue } from '../../context/WindowManagerContext';
import { WindowManagerProvider } from '../../context/WindowManagerProvider';
import {
  closeSettingsOverlay,
  isSettingsEntryPath,
  useSettingsOverlayOrigin,
} from '../../lib/settingsOverlay';
import { SettingsOverlayNavigationBoundary } from '../settings/SettingsOverlayNavigationBoundary';
import { WindowLayer } from './WindowLayer';

const WINDOW_MESSAGE = 'Discard window draft?';
const ROUTE_MESSAGE = 'Discard route draft?';

const api = vi.hoisted(() => ({
  getShowPages: vi.fn(),
  getSessionResult: vi.fn(),
  connectWorkbenchEvents: vi.fn(),
}));

vi.mock('../../context/DockContext', () => ({ useDock: () => ({ order: [], pins: [] }) }));
vi.mock('../../context/StandaloneAppTabContext', () => ({ useStandaloneAppTab: () => false }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../useShowPages', () => ({ useShowPageInventory: () => ({ pages: [] }) }));
vi.mock('../workbench/ShowPageAnnotationHost', () => ({
  ShowPageAnnotationHost: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// A window body that is permanently dirty and tries to claim the route blocker,
// plus the title bar's route action (open-chat), which must still pre-flight
// against the route behind the window.
vi.mock('./AppWindow', () => ({
  AppWindow: () => {
    useUnsavedChanges(WINDOW_MESSAGE);
    const authorizeRouteAction = useUnsavedChangesActionGuard();
    return (
      <div data-window-id="win_1" data-testid="window-body">
        <button type="button" onClick={() => authorizeRouteAction()}>window-route-action</button>
      </div>
    );
  },
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const RouteDraft = () => {
  useUnsavedChanges(ROUTE_MESSAGE);
  return null;
};

let manager: WindowManagerValue | null = null;

// AppShell's arrangement: the layer is hidden exactly while Settings is open.
const Shell = ({ dirtyRoute }: { dirtyRoute: boolean }) => {
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayOrigin(location);
  const value = useWindowManager();
  useEffect(() => {
    manager = value;
  }, [value]);
  return (
    <SettingsOverlayNavigationBoundary desktop>
      {dirtyRoute ? <RouteDraft /> : null}
      {origin ? (
        <button type="button" onClick={() => closeSettingsOverlay(navigate, origin)}>close-settings</button>
      ) : (
        <Link to="/settings/general">open-settings</Link>
      )}
      <Link to="/chat/ses_2">sidebar-chat-link</Link>
      <div data-testid="location">{location.pathname}</div>
      <WindowLayer active={!isSettingsEntryPath(location.pathname)} />
    </SettingsOverlayNavigationBoundary>
  );
};

const renderShell = ({ dirtyRoute = false } = {}) => {
  const router = createMemoryRouter([{
    path: '*',
    element: (
      <UnsavedChangesProvider>
        <I18nextProvider i18n={i18n}>
          <WindowManagerProvider onWindowForeground={() => undefined}>
            <Shell dirtyRoute={dirtyRoute} />
          </WindowManagerProvider>
        </I18nextProvider>
      </UnsavedChangesProvider>
    ),
  }], { initialEntries: ['/chat/ses_1'] });
  render(<RouterProvider router={router} />);
  act(() => void manager!.openApp('editor'));
  expect(screen.getByTestId('window-body')).toBeTruthy();
};

let confirmSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  confirmSpy = vi.fn(() => false);
  vi.stubGlobal('confirm', confirmSpy);
  window.history.replaceState(null, '');
  api.getShowPages.mockReset().mockResolvedValue([]);
  api.getSessionResult.mockReset().mockResolvedValue({ status: null, session: null });
  api.connectWorkbenchEvents.mockReset().mockReturnValue(vi.fn());
  manager = null;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('WindowLayer and the route unsaved-changes blocker', () => {
  it('never prompts for a dirty window on Settings open, close or route navigation', async () => {
    const user = userEvent.setup();
    renderShell();

    await user.click(screen.getByRole('link', { name: 'open-settings' }));
    expect(screen.getByTestId('location').textContent).toBe('/settings/general');
    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(screen.getByTestId('location').textContent).toBe('/chat/ses_1');

    await user.click(screen.getByRole('link', { name: 'open-settings' }));
    await user.click(screen.getByRole('link', { name: 'sidebar-chat-link' }));
    expect(screen.getByTestId('location').textContent).toBe('/chat/ses_2');

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId('window-body')).toBeTruthy();
  });

  it('still pre-flights a window route action against the dirty route behind it', async () => {
    const user = userEvent.setup();
    renderShell({ dirtyRoute: true });

    await user.click(screen.getByRole('button', { name: 'window-route-action' }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy).toHaveBeenCalledWith(ROUTE_MESSAGE);
  });
});
