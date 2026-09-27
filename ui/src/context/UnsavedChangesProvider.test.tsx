/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import {
  Link,
  Navigate,
  Outlet,
  Route,
  RouterProvider,
  createMemoryRouter,
  createRoutesFromElements,
  useLocation,
  useNavigate,
} from 'react-router-dom';

import { SettingsOverlayNavigationBoundary } from '../components/settings/SettingsOverlayNavigationBoundary';
import { SettingsOverlayRouteSurface } from '../components/settings/SettingsOverlayRouteSurface';
import {
  closeSettingsOverlay,
  useSettingsOverlayContext,
  useSettingsOverlayOrigin,
} from '../lib/settingsOverlay';
import { SETTINGS_MENU_PLACEMENT_STORAGE_KEY } from '../lib/settingsMenuPlacement';
import { UnsavedChangesProvider } from './UnsavedChangesProvider';
import { useUnsavedChanges } from './useUnsavedChanges';

const DIRTY_MESSAGE = 'Discard unsaved changes?';

// The route's claim follows its draft, exactly as a real editor's does, so the
// assertions below can also prove the draft itself survived.
const DirtyEditorRoute = () => {
  const [draft, setDraft] = useState('');
  useUnsavedChanges(draft ? DIRTY_MESSAGE : null);
  return (
    <input
      aria-label="editor draft"
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
    />
  );
};

// AppShell's sidebar toggle: a link into Settings while it is closed, and the
// overlay's own exit while it is open.
const SettingsToggle = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayOrigin(location);
  return origin ? (
    <button
      type="button"
      data-settings-toggle="true"
      onClick={() => closeSettingsOverlay(navigate, origin)}
    >
      shell-settings
    </button>
  ) : (
    <Link to="/settings/general" data-settings-toggle="true">shell-settings</Link>
  );
};

const SettingsFrame = () => {
  const navigate = useNavigate();
  const origin = useSettingsOverlayContext();
  return (
    <section>
      <button type="button" onClick={() => origin && closeSettingsOverlay(navigate, origin)}>
        close-settings
      </button>
      <Link to="/settings/replies">replies-section</Link>
      <Outlet />
    </section>
  );
};

const Root = () => (
  <UnsavedChangesProvider>
    <SettingsOverlayNavigationBoundary desktop>
      <SettingsToggle />
      {/* Shell chrome outside the overlay, live beside Settings in both menu placements. */}
      <aside>
        <Link to="/chat/ses_1">sidebar-chat-link</Link>
      </aside>
      <SettingsOverlayRouteSurface fallbackElement={<Navigate to="/" replace />}>
        <Route path="/apps/editor" element={<DirtyEditorRoute />} />
        <Route path="/chat/:sessionId" element={<div data-testid="chat">chat</div>} />
        <Route path="/settings" element={<SettingsFrame />}>
          <Route path="general" element={<div>general-settings</div>} />
          <Route path="replies" element={<div>replies-settings</div>} />
        </Route>
      </SettingsOverlayRouteSurface>
    </SettingsOverlayNavigationBoundary>
  </UnsavedChangesProvider>
);

const renderApp = () => {
  const router = createMemoryRouter(
    createRoutesFromElements(<Route path="*" element={<Root />} />),
    { initialEntries: ['/apps/editor'] },
  );
  render(<RouterProvider router={router} />);
  return router;
};

const settingsOpen = () => document.querySelector('[data-settings-overlay="true"]') !== null;
const draft = () => (screen.getByLabelText('editor draft') as HTMLInputElement).value;

let confirmSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  confirmSpy = vi.fn(() => false);
  vi.stubGlobal('confirm', confirmSpy);
  window.localStorage.clear();
  // Closing Settings reads the real history stack to choose between a pop and a
  // replace; each test that cares sets `idx` itself.
  window.history.replaceState(null, '');
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

describe('UnsavedChangesProvider with Settings closed', () => {
  it('prompts before a navigation leaves the dirty route', async () => {
    const user = userEvent.setup();
    const router = renderApp();
    await user.type(screen.getByLabelText('editor draft'), 'draft');

    await user.click(screen.getByRole('link', { name: 'sidebar-chat-link' }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy).toHaveBeenCalledWith(DIRTY_MESSAGE);
    expect(router.state.location.pathname).toBe('/apps/editor');
    expect(draft()).toBe('draft');
  });
});

// Both menu placements render the same overlay over the same blocker; they
// differ only in what an outside click means, which is why each is driven here.
describe.each(['standalone', 'inline'] as const)(
  'UnsavedChangesProvider with Settings open (%s menu)',
  (placement) => {
    beforeEach(() => {
      window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, placement);
    });

    const openSettingsOverDirtyEditor = async (user: ReturnType<typeof userEvent.setup>) => {
      const router = renderApp();
      await user.type(screen.getByLabelText('editor draft'), 'draft');
      window.history.replaceState({ idx: 0 }, '');
      await user.click(screen.getByRole('link', { name: 'shell-settings' }));
      expect(settingsOpen()).toBe(true);
      return router;
    };

    it.each([
      // Two entries above the origin: the exit pops back to the origin entry.
      ['by history pop', { idx: 2 }],
      // No history index: the exit replaces onto the origin under a new key.
      ['by replace', null],
    ] as const)('opens, changes section and closes %s without a prompt', async (_, historyAtClose) => {
      const user = userEvent.setup();
      const router = await openSettingsOverDirtyEditor(user);

      await user.click(screen.getByRole('link', { name: 'replies-section' }));
      expect(screen.getByText('replies-settings')).toBeTruthy();
      window.history.replaceState(historyAtClose, '');
      await user.click(screen.getByRole('button', { name: 'close-settings' }));

      await waitFor(() => expect(settingsOpen()).toBe(false));
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(router.state.location.pathname).toBe('/apps/editor');
      expect(draft()).toBe('draft');

      // Reopening over the same route and leaving by the shell toggle is just as quiet.
      await user.click(screen.getByRole('link', { name: 'shell-settings' }));
      await user.click(screen.getByRole('button', { name: 'shell-settings' }));
      await waitFor(() => expect(settingsOpen()).toBe(false));
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(draft()).toBe('draft');
    });

    it('prompts once for a shell navigation and keeps everything in place on cancel', async () => {
      const user = userEvent.setup();
      const router = await openSettingsOverDirtyEditor(user);

      await user.click(screen.getByRole('link', { name: 'sidebar-chat-link' }));

      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(confirmSpy).toHaveBeenCalledWith(DIRTY_MESSAGE);
      expect(router.state.location.pathname).toBe('/settings/general');
      expect(settingsOpen()).toBe(true);
      expect(screen.getByText('general-settings')).toBeTruthy();
      expect(screen.queryByTestId('chat')).toBeNull();
      expect(draft()).toBe('draft');
    });

    it('navigates and closes Settings when the discard is confirmed', async () => {
      const user = userEvent.setup();
      const router = await openSettingsOverDirtyEditor(user);
      confirmSpy.mockReturnValue(true);

      await user.click(screen.getByRole('link', { name: 'sidebar-chat-link' }));

      expect(confirmSpy).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(router.state.location.pathname).toBe('/chat/ses_1'));
      expect(screen.getByTestId('chat')).toBeTruthy();
      await waitFor(() => expect(settingsOpen()).toBe(false));
      expect(screen.queryByLabelText('editor draft')).toBeNull();
    });
  },
);
