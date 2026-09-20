/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  Navigate,
  Route,
  RouterProvider,
  createMemoryRouter,
  createRoutesFromElements,
  useLocation,
  useNavigate,
} from 'react-router-dom';

import { SettingsOverlayRouteSurface } from '../components/settings/SettingsOverlayRouteSurface';
import {
  closeSettingsOverlay,
  settingsOverlayOpenState,
  useSettingsOverlayOrigin,
} from '../lib/settingsOverlay';
import { UnsavedChangesProvider } from './UnsavedChangesProvider';
import { useUnsavedChanges } from './useUnsavedChanges';

const DIRTY_MESSAGE = 'Discard unsaved changes?';

const DirtyEditorRoute = () => {
  const navigate = useNavigate();
  useUnsavedChanges(DIRTY_MESSAGE);
  return (
    <main>
      <div data-testid="editor">editor</div>
      <button type="button" onClick={() => navigate('/chat/ses_1')}>leave-editor</button>
    </main>
  );
};

const OpenSettings = () => {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <button
      type="button"
      onClick={() => navigate('/settings/general', { state: settingsOverlayOpenState(location) })}
    >
      open-settings
    </button>
  );
};

// Stands in for AppShell's `onWindowForeground` exit: the same
// `closeSettingsOverlay` call, made from shell chrome rather than from a
// control inside the surface.
const WindowForegroundExit = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayOrigin(location);
  if (!origin) return null;
  return (
    <button type="button" onClick={() => closeSettingsOverlay(navigate, origin)}>
      leave-for-window
    </button>
  );
};

const Root = () => (
  <UnsavedChangesProvider>
    <OpenSettings />
    <WindowForegroundExit />
    <SettingsOverlayRouteSurface fallbackElement={<Navigate to="/" replace />}>
      <Route path="/apps/editor" element={<DirtyEditorRoute />} />
      <Route path="/chat/:sessionId" element={<div data-testid="chat">chat</div>} />
      <Route path="/settings/general" element={<div data-testid="settings">settings</div>} />
    </SettingsOverlayRouteSurface>
  </UnsavedChangesProvider>
);

const renderApp = () => render(
  <RouterProvider
    router={createMemoryRouter(
      createRoutesFromElements(<Route path="*" element={<Root />} />),
      { initialEntries: ['/apps/editor'] },
    )}
  />,
);

let confirmSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  confirmSpy = vi.fn(() => false);
  vi.stubGlobal('confirm', confirmSpy);
  window.localStorage.clear();
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({
    matches: true,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('UnsavedChangesProvider and the Settings overlay', () => {
  it('leaves the overlay without a discard prompt while the dirty route is retained', () => {
    renderApp();
    fireEvent.click(screen.getByText('open-settings'));
    expect(screen.getByTestId('settings')).toBeTruthy();
    // The dirty route did not unmount — it is retained behind the surface, with
    // its draft intact. That is what makes the exit below lossless.
    expect(screen.getByTestId('editor')).toBeTruthy();

    fireEvent.click(screen.getByText('leave-for-window'));

    // Closing the overlay returns to the route it was opened over, so there is
    // nothing to discard and nothing to confirm. An exit that cannot be refused
    // is also an exit its caller cannot half-apply: whatever brought the user
    // out of Settings — a window coming forward, the toggle, Escape — completes
    // whole. Should a future change re-arm the registration behind the surface,
    // this fails rather than silently reintroducing that split.
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.queryByTestId('settings')).toBeNull();
    expect(screen.getByTestId('editor')).toBeTruthy();
  });

  it('still prompts when the navigation really does leave the dirty route', () => {
    renderApp();

    fireEvent.click(screen.getByText('leave-editor'));

    expect(confirmSpy).toHaveBeenCalledWith(DIRTY_MESSAGE);
    // Cancelled, so the editor keeps the route it never left.
    expect(screen.queryByTestId('chat')).toBeNull();
    expect(screen.getByTestId('editor')).toBeTruthy();
  });
});
