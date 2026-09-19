/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useEffect, useRef, useState } from 'react';
import {
  Link,
  MemoryRouter,
  Navigate,
  Outlet,
  Route,
  RouterProvider,
  Routes,
  createMemoryRouter,
  useLocation,
  useNavigate,
} from 'react-router-dom';

import {
  closeSettingsOverlay,
  useSettingsOverlayOrigin,
  useSettingsOverlayContext,
} from '@/lib/settingsOverlay';
import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { SettingsOverlayNavigationBoundary } from './SettingsOverlayNavigationBoundary';
import { isForegroundFocusOwner, SettingsOverlayRouteSurface } from './SettingsOverlayRouteSurface';

let chatMounts = 0;
let chatUnmounts = 0;

const RetainedModalEditor = () => {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('保留');
  const inputRef = useRef<HTMLInputElement | null>(null);
  const selectionRef = useRef({ start: 2, end: 2 });

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>open-retained-editor</button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          aria-describedby={undefined}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            const input = inputRef.current;
            input?.focus();
            input?.setSelectionRange(selectionRef.current.start, selectionRef.current.end);
          }}
        >
          <DialogTitle className="sr-only">retained editor</DialogTitle>
          <input
            ref={inputRef}
            aria-label="retained editor input"
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              selectionRef.current = {
                start: event.target.selectionStart ?? event.target.value.length,
                end: event.target.selectionEnd ?? event.target.value.length,
              };
            }}
            onSelect={(event) => {
              selectionRef.current = {
                start: event.currentTarget.selectionStart ?? 0,
                end: event.currentTarget.selectionEnd ?? 0,
              };
            }}
          />
          <button
            type="button"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => navigate('/settings/replies')}
          >
            open-settings-from-editor
          </button>
        </DialogContent>
      </Dialog>
    </>
  );
};

const ChatProbe = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const routeSurfaceActive = useRouteSurfaceActive();
  const [count, setCount] = useState(0);

  useEffect(() => {
    chatMounts += 1;
    return () => {
      chatUnmounts += 1;
    };
  }, []);

  useEffect(() => {
    if (!routeSurfaceActive) navigate('/escaped');
  }, [navigate, routeSurfaceActive]);

  useEffect(() => {
    const maintenance = (location.state as { maintenance?: string } | null)?.maintenance;
    if (!routeSurfaceActive && maintenance === 'pending') {
      navigate(`${location.pathname}${location.search}${location.hash}`, {
        replace: true,
        state: { maintenance: 'done' },
      });
    }
  }, [location, navigate, routeSurfaceActive]);

  return (
    <main>
      <div data-testid="chat-location">{`${location.pathname}${location.search}${location.hash}`}</div>
      <div data-testid="chat-count">{count}</div>
      <div data-testid="chat-active">{String(routeSurfaceActive)}</div>
      <div data-testid="chat-maintenance">
        {(location.state as { maintenance?: string } | null)?.maintenance ?? 'none'}
      </div>
      <button type="button" onClick={() => setCount((value) => value + 1)}>increment-chat</button>
      <RetainedModalEditor />
      <button type="button" onClick={() => navigate('/settings/replies')} onMouseDown={(event) => event.preventDefault()}>
        open-settings-preserving-focus
      </button>
      <Link to="/settings/replies">open-settings</Link>
      <Link to="/settings/diagnostics">open-diagnostics</Link>
      <Link to="/doctor">open-legacy-settings</Link>
    </main>
  );
};

const SettingsFrame = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayContext();
  const [count, setCount] = useState(0);

  return (
    <aside>
      <button
        type="button"
        onClick={() => {
          if (origin) closeSettingsOverlay(navigate, origin);
          else navigate('/');
        }}
      >
        close-settings
      </button>
      <button type="button" onClick={() => navigate('/settings/advanced')}>open-advanced</button>
      <button type="button" onClick={() => setCount((value) => value + 1)}>increment-settings</button>
      <div data-testid="settings-count">{count}</div>
      <div data-testid="settings-location">{location.pathname}</div>
      <Outlet />
    </aside>
  );
};

const settingsRoute = () => (
  <Route path="/settings" element={<SettingsFrame />}>
    <Route path="replies" element={<div>replies-settings</div>} />
    <Route path="advanced" element={<div>advanced-settings</div>} />
    <Route path="diagnostics" element={<div>diagnostics-settings</div>} />
  </Route>
);

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
    <Link to="/settings/replies" data-settings-toggle="true">shell-settings</Link>
  );
};

const Harness = ({ desktop }: { desktop: boolean }) => (
  <SettingsOverlayNavigationBoundary desktop={desktop}>
    <SettingsToggle />
    <SettingsOverlayRouteSurface fallbackElement={<Navigate to="/" replace />}>
      <Route path="/chat/:sessionId" element={<ChatProbe />} />
      <Route path="/escaped" element={<div>escaped-route</div>} />
      {settingsRoute()}
      <Route path="/doctor" element={<Navigate to="/settings/diagnostics" replace />} />
      <Route path="/" element={<div>workbench</div>} />
    </SettingsOverlayRouteSurface>
  </SettingsOverlayNavigationBoundary>
);

const RoutedHarness = ({ desktop = true }: { desktop?: boolean }) => (
  <Routes>
    <Route path="*" element={<Harness desktop={desktop} />} />
  </Routes>
);

const RemountingHarness = () => {
  const location = useLocation();
  const guardKey = location.pathname.startsWith('/settings/diagnostics') ? 'diagnostics' : 'default';
  return <Harness key={guardKey} desktop />;
};

beforeEach(() => {
  chatMounts = 0;
  chatUnmounts = 0;
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

describe('SettingsOverlayRouteSurface', () => {
  it('recognizes retained modal producers without admitting ordinary windows or popovers', () => {
    const explicitModal = document.createElement('div');
    explicitModal.setAttribute('role', 'dialog');
    explicitModal.setAttribute('aria-modal', 'true');
    const explicitEditor = document.createElement('input');
    explicitModal.append(explicitEditor);
    document.body.append(explicitModal);

    const sharedModal = document.createElement('div');
    sharedModal.setAttribute('role', 'dialog');
    sharedModal.setAttribute('data-state', 'open');
    sharedModal.setAttribute('aria-labelledby', 'dialog-title');
    const sharedEditor = document.createElement('input');
    sharedModal.append(sharedEditor);
    document.body.append(sharedModal);

    const appWindow = document.createElement('div');
    appWindow.setAttribute('role', 'dialog');
    appWindow.setAttribute('data-window-id', 'win-1');
    appWindow.setAttribute('aria-label', 'Editor');
    const windowEditor = document.createElement('input');
    appWindow.append(windowEditor);
    document.body.append(appWindow);

    const popover = document.createElement('div');
    popover.setAttribute('role', 'dialog');
    popover.setAttribute('data-state', 'open');
    popover.setAttribute('aria-label', 'Inbox');
    const popoverEditor = document.createElement('input');
    popover.append(popoverEditor);
    document.body.append(popover);

    try {
      expect(isForegroundFocusOwner(explicitEditor)).toBe(true);
      expect(isForegroundFocusOwner(sharedEditor)).toBe(true);
      expect(isForegroundFocusOwner(windowEditor)).toBe(false);
      expect(isForegroundFocusOwner(popoverEditor)).toBe(false);
    } finally {
      explicitModal.remove();
      sharedModal.remove();
      appWindow.remove();
      popover.remove();
    }
  });

  it('preserves the Chat route with a data router', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter([
      { path: '*', element: <Harness desktop /> },
    ], {
      initialEntries: ['/chat/ses_1'],
    });
    render(<RouterProvider router={router} />);

    await user.click(screen.getByRole('link', { name: 'shell-settings' }));

    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1');

    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'shell-settings' }));
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1');
  });

  it('keeps a remounted retained modal editor focused for continued Unicode input', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('button', { name: 'open-retained-editor' }));
    const editorInput = screen.getByRole('textbox', { name: 'retained editor input' });
    expect(document.activeElement).toBe(editorInput);
    editorInput.setSelectionRange(1, 2);
    fireEvent.select(editorInput);
    expect(editorInput.selectionStart).toBe(1);
    expect(editorInput.selectionEnd).toBe(2);

    // The mousedown guard models a retained modal's explicit Settings handoff:
    // navigation changes the foreground route without making the shell button
    // the editor's new owner of focus.
    await user.click(screen.getByRole('button', { name: 'open-settings-from-editor' }));
    await user.click(screen.getByRole('button', { name: 'close-settings' }));

    const resumedInput = await screen.findByRole('textbox', { name: 'retained editor input' });
    expect(resumedInput).not.toBe(editorInput);
    await waitFor(() => expect(document.activeElement).toBe(resumedInput));
    expect(document.activeElement).toBe(resumedInput);
    expect((resumedInput as HTMLInputElement).selectionStart).toBe(1);
    expect((resumedInput as HTMLInputElement).selectionEnd).toBe(2);
    await user.keyboard('x');
    expect((resumedInput as HTMLInputElement).value).toBe('保x');
    expect(document.activeElement).toBe(resumedInput);
    await user.keyboard('{End}续写🌱');
    expect((resumedInput as HTMLInputElement).value).toBe('保x续写🌱');
  });

  it('does not let a stale close callback focus the old origin after Settings reopens', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    const queuedFrames: FrameRequestCallback[] = [];
    const requestFrame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      queuedFrames.push(callback);
      return queuedFrames.length;
    });
    const cancelFrame = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined);

    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    await waitFor(() => expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull());
    expect(requestFrame).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    expect(cancelFrame).toHaveBeenCalledWith(1);

    queuedFrames[0](performance.now());
    expect(screen.getByRole('dialog', { name: 'nav.settings' }).contains(document.activeElement)).toBe(true);

    requestFrame.mockRestore();
    cancelFrame.mockRestore();
  });

  it('keeps the background route mounted across Settings navigation and close', async () => {
    const user = userEvent.setup();
    const view = render(
      <MemoryRouter initialEntries={[{
        pathname: '/chat/ses_1',
        search: '?view=chat',
        hash: '#tail',
        state: { maintenance: 'pending' },
      }]}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('button', { name: 'increment-chat' }));
    expect(screen.getByTestId('chat-count').textContent).toBe('1');
    expect(chatMounts).toBe(1);

    const settingsIngress = screen.getByRole('link', { name: 'shell-settings' });
    await user.click(settingsIngress);
    expect(screen.getByText('replies-settings')).toBeTruthy();
    const dialog = screen.getByRole('dialog', { name: 'nav.settings' });
    expect(dialog).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole('button', { name: 'close-settings' }),
    ));
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1?view=chat#tail');
    expect(screen.getByTestId('chat-count').textContent).toBe('1');
    expect(screen.getByTestId('chat-active').textContent).toBe('false');
    expect(screen.getByTestId('chat-maintenance').textContent).toBe('done');
    expect(screen.queryByText('escaped-route')).toBeNull();
    expect(chatMounts).toBe(1);
    expect(chatUnmounts).toBe(0);

    await user.click(screen.getByRole('button', { name: 'open-advanced' }));
    expect(screen.getByText('advanced-settings')).toBeTruthy();
    expect(screen.getByTestId('chat-count').textContent).toBe('1');
    expect(chatMounts).toBe(1);
    expect(chatUnmounts).toBe(0);

    await user.click(screen.getByRole('button', { name: 'increment-settings' }));
    expect(screen.getByTestId('settings-count').textContent).toBe('1');
    view.rerender(
      <MemoryRouter initialEntries={['/chat/ses_1?view=chat#tail']}>
        <RoutedHarness desktop={false} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('settings-count').textContent).toBe('1');

    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull();
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1?view=chat#tail');
    expect(screen.getByTestId('chat-count').textContent).toBe('1');
    expect(screen.getByTestId('chat-active').textContent).toBe('true');
    expect(screen.getByTestId('chat-maintenance').textContent).toBe('done');
    expect(chatMounts).toBe(1);
    expect(chatUnmounts).toBe(0);
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole('link', { name: 'shell-settings' }),
    ));
  });

  it('carries the origin through a guard remount', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <Routes>
          <Route path="*" element={<RemountingHarness />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'open-diagnostics' }));
    expect(await screen.findByText('diagnostics-settings')).toBeTruthy();
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(await screen.findByTestId('chat-location')).toBeTruthy();
  });

  it('closes when the persistent Settings toggle is clicked again', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();
    expect(document.body.style.pointerEvents).not.toBe('none');
    expect(document.querySelector('[data-dialog-surface-backdrop="true"]')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'shell-settings' }));

    await waitFor(() => expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull());
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1');
  });

  it('keeps legacy redirects out of origins while preserving real ingress origins', async () => {
    const user = userEvent.setup();
    const view = render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'open-legacy-settings' }));
    expect(await screen.findByText('diagnostics-settings')).toBeTruthy();
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(await screen.findByTestId('chat-location')).toBeTruthy();

    view.unmount();
    render(
      <MemoryRouter initialEntries={['/doctor']}>
        <RoutedHarness />
      </MemoryRouter>,
    );
    expect(await screen.findByText('diagnostics-settings')).toBeTruthy();
    expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull();
  });

  it('renders a direct Settings URL as the primary route', () => {
    render(
      <MemoryRouter initialEntries={['/settings/replies']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    expect(screen.getByText('replies-settings')).toBeTruthy();
    expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull();
    expect(screen.queryByTestId('chat-count')).toBeNull();
  });
});
