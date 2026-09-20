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
import { ShellSidebarContext } from '@/context/ShellSidebarContext';
import { SETTINGS_MENU_PLACEMENT_STORAGE_KEY } from '@/lib/settingsMenuPlacement';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { SettingsOverlayNavigationBoundary } from './SettingsOverlayNavigationBoundary';
import { SettingsOverlayRouteSurface } from './SettingsOverlayRouteSurface';

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

type RetainedFocusOwnerKind = 'explicit-modal' | 'app-window' | 'popover';

const RetainedFocusOwner = ({ kind }: { kind: RetainedFocusOwnerKind }) => {
  const active = useRouteSurfaceActive();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (active && open) inputRef.current?.focus();
  }, [active, open]);

  const dialogProps = kind === 'explicit-modal'
    ? { 'aria-modal': 'true' }
    : kind === 'app-window'
      ? { 'data-window-id': 'retained-window' }
      : { 'data-state': 'open', 'aria-label': 'retained popover' };
  const label = `retained ${kind} input`;

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>open-{kind}</button>
      {open && active ? (
        <div role="dialog" {...dialogProps}>
          <input ref={inputRef} aria-label={label} defaultValue="保留" />
          <button
            type="button"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => navigate('/settings/replies')}
          >
            open-settings-from-{kind}
          </button>
        </div>
      ) : null}
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
      <RetainedFocusOwner kind="explicit-modal" />
      <RetainedFocusOwner kind="app-window" />
      <RetainedFocusOwner kind="popover" />
      <button type="button" onClick={() => navigate('/settings/replies')} onMouseDown={(event) => event.preventDefault()}>
        open-settings-preserving-focus
      </button>
      <Link to="/settings/replies">open-settings</Link>
      <Link to="/settings/diagnostics">open-diagnostics</Link>
      <Link to="/doctor">open-legacy-settings</Link>
    </main>
  );
};

const SetupProbe = () => <Link to="/settings/models">open-model-hub</Link>;

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
    <Route path="models" element={<div>models-settings</div>} />
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
    {/* Shell chrome that lives OUTSIDE the overlay. Inline, the app sidebar is
        still on screen beside Settings and still live, so what an outside
        interaction means stops being hypothetical — and the answer depends on
        whether it landed in that sidebar, which is why these are nested the way
        the shell nests them. */}
    <aside data-app-sidebar="true">
      <button type="button" data-sidebar-resizer="true">shell-resizer</button>
      <button type="button">sidebar-idle</button>
      <Link to="/chat/ses_2">sidebar-chat-link</Link>
    </aside>
    <button type="button">shell-elsewhere</button>
    <SettingsOverlayRouteSurface fallbackElement={<Navigate to="/" replace />}>
      <Route path="/setup" element={<SetupProbe />} />
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

const settleDeferredFocus = async () => {
  await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
  await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
};

beforeEach(() => {
  chatMounts = 0;
  chatUnmounts = 0;
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

describe('SettingsOverlayRouteSurface', () => {
  it('opens Model Hub over setup on mobile and closes back to the wizard', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/setup']}>
        <RoutedHarness desktop={false} />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'open-model-hub' }));
    expect(screen.getByText('models-settings')).toBeTruthy();
    expect(screen.getByRole('dialog')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(screen.getByRole('link', { name: 'open-model-hub' })).toBeTruthy();
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

  it('maintains only the retained origin through data-router scoped replacement', async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter([{ path: '*', element: <Harness desktop /> }], {
      initialEntries: [{ pathname: '/chat/ses_1', search: '?view=chat', hash: '#tail', state: { maintenance: 'pending' } }],
    });
    render(<RouterProvider router={router} />);
    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    await waitFor(() => expect(screen.getByTestId('chat-maintenance').textContent).toBe('done'));
    expect(router.state.location.pathname).toBe('/settings/replies');
    expect(screen.queryByText('escaped-route')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(router.state.location.pathname).toBe('/chat/ses_1');
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1?view=chat#tail');
    expect(screen.getByTestId('chat-maintenance').textContent).toBe('done');
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
    await settleDeferredFocus();
    expect(document.activeElement).toBe(resumedInput);
    expect((resumedInput as HTMLInputElement).selectionStart).toBe(1);
    expect((resumedInput as HTMLInputElement).selectionEnd).toBe(2);
    await user.keyboard('x');
    expect((resumedInput as HTMLInputElement).value).toBe('保x');
    expect(document.activeElement).toBe(resumedInput);
    await user.keyboard('{End}续写🌱');
    expect((resumedInput as HTMLInputElement).value).toBe('保x续写🌱');
  });

  it('keeps an explicit retained modal as the return-focus owner after its editor remounts', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('button', { name: 'open-explicit-modal' }));
    const editor = screen.getByRole('textbox', { name: 'retained explicit-modal input' });
    expect(document.activeElement).toBe(editor);
    await user.click(screen.getByRole('button', { name: 'open-settings-from-explicit-modal' }));
    await user.click(screen.getByRole('button', { name: 'close-settings' }));

    const resumedEditor = await screen.findByRole('textbox', { name: 'retained explicit-modal input' });
    await settleDeferredFocus();
    expect(document.activeElement).toBe(resumedEditor);
  });

  it.each([
    ['app-window', 'retained app-window input'],
    ['popover', 'retained popover input'],
  ] as const)('falls back from a retained non-modal %s instead of restoring its input', async (kind, inputName) => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('button', { name: `open-${kind}` }));
    const input = screen.getByRole('textbox', { name: inputName });
    expect(document.activeElement).toBe(input);
    await user.click(screen.getByRole('button', { name: `open-settings-from-${kind}` }));
    await user.click(screen.getByRole('button', { name: 'close-settings' }));

    await screen.findByRole('textbox', { name: inputName });
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole('link', { name: 'shell-settings' }),
    ));
    expect(document.activeElement).not.toBe(screen.getByRole('textbox', { name: inputName }));
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

  // Standalone Settings replaces the app sidebar, so it starts at the screen
  // edge with nothing on that side to divide from. Inline Settings opens beside
  // a sidebar that is still there, so it takes the primitive's own
  // `--app-sidebar-w` offset — the same variable the sidebar sizes itself with,
  // which is what keeps the two edges together while it is dragged.
  it.each([
    ['standalone', 'md:left-0', 'md:left-[var(--app-sidebar-w)]', false],
    ['inline', 'md:left-[var(--app-sidebar-w)]', 'md:left-0', true],
  ] as const)('starts the %s surface at the right edge', async (
    placement,
    offset,
    rejected,
    dividedFromSidebar,
  ) => {
    const user = userEvent.setup();
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, placement);
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    const surface = document.querySelector('[data-settings-overlay="true"]') as HTMLElement;

    expect(surface.getAttribute('data-settings-menu-placement')).toBe(placement);
    // Exact class tokens: `md:border-l-0` would satisfy a substring match for
    // `md:border-l` and quietly invert what this asserts.
    expect(surface.classList.contains(offset)).toBe(true);
    expect(surface.classList.contains(rejected)).toBe(false);
    expect(surface.classList.contains('md:border-l')).toBe(dividedFromSidebar);
  });

  it('leaves a live sidebar to its own affordances, and treats the rest of the shell as a way out', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, 'inline');
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();

    // Grabbing the divider moves this surface's OWN left edge. Dismissing on it
    // would close the thing the drag is laying out.
    await user.click(screen.getByRole('button', { name: 'shell-resizer' }));
    expect(document.querySelector('[data-settings-overlay="true"]')).toBeTruthy();

    // Nor is the sidebar's own quiet space a dismissal: inline puts these two
    // surfaces side by side, so the sidebar is a neighbour, not "outside".
    await user.click(screen.getByRole('button', { name: 'sidebar-idle' }));
    expect(document.querySelector('[data-settings-overlay="true"]')).toBeTruthy();

    // A sidebar link is the one thing that does take the user out — by its own
    // navigation, which is exactly one navigation. Dismissal must not also fire
    // here: `closeSettingsOverlay` traverses history asynchronously and would
    // race this synchronous push back to the retained origin.
    await user.click(screen.getByRole('link', { name: 'sidebar-chat-link' }));
    await waitFor(() => expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull());
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_2');

    // Everything outside that sidebar is still a way out, landing back on the
    // retained origin rather than anywhere the shell happened to be.
    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'shell-elsewhere' }));
    await waitFor(() => expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull());
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_2');
  });

  // Some shells draw no app sidebar at all — the setup wizard, a single-app tab.
  // Inline over one of those would offset Settings past an empty strip and give
  // it a rail narrowed for a neighbour that does not exist, so the stored
  // preference is simply not in force there. Which shells those are is the
  // shell's own business: this surface reads the answer it publishes rather
  // than trying to recognise them by pathname.
  it('opens standalone where the shell draws no sidebar, even when inline is stored', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, 'inline');
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <ShellSidebarContext.Provider value={false}>
          <RoutedHarness />
        </ShellSidebarContext.Provider>
      </MemoryRouter>,
    );

    // An ordinary workbench route, so nothing about the path suggests the
    // answer: only the shell's own claim does.
    await user.click(screen.getByRole('link', { name: 'open-settings' }));
    const surface = document.querySelector('[data-settings-overlay="true"]') as HTMLElement;

    expect(surface.getAttribute('data-settings-menu-placement')).toBe('standalone');
    expect(surface.classList.contains('md:left-0')).toBe(true);
    expect(surface.classList.contains('md:left-[var(--app-sidebar-w)]')).toBe(false);
    expect(surface.classList.contains('md:border-l')).toBe(false);
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
