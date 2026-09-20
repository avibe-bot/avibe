/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import type { ReactNode } from 'react';
import { closeSettingsOverlay, useSettingsOverlayOrigin } from '../lib/settingsOverlay';

import { APP_TAB_PARAM } from '../apps/appLaunch';
import {
  APP_SHELL_SCROLL_ID,
  clearMobileProjectsListSnapshot,
  holdMobileProjectsListForChatReturn,
  readMobileProjectsListSnapshot,
} from '../lib/mobileProjectsListMemory';
import { selectLanguage } from '../lib/useLanguageSelection';
import {
  SETTINGS_MENU_PLACEMENT_STORAGE_KEY,
  useStandaloneSettingsMenu,
} from '../lib/settingsMenuPlacement';
import { AppShell } from './AppShell';

const viewport = vi.hoisted(() => {
  const state = { isDesktop: false };
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: state.isDesktop,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
  return state;
});

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getMemorySettings: vi.fn(),
  getVersion: vi.fn(),
}));
const status = vi.hoisted(() => ({ state: 'ready' as const }));
const inbox = vi.hoisted(() => ({ totalUnread: 0 }));
/** One per test, not one per file: a language operation belongs to the i18n
 *  instance and outlives any render root, so a pick made in one test would
 *  follow a shared instance into the next. */
const makeI18n = () => {
  const instance = {
    language: 'en',
    options: { resources: { en: {}, zh: {} } },
    changeLanguage: vi.fn(async (code: string) => {
      instance.language = code;
    }),
  };
  return instance;
};

/** What `useTranslation` actually hands a component: not the instance but a copy
 *  of it, made fresh on every language change, keeping the instance itself as
 *  `__original`. The shell holds one of these, which is why a language change is
 *  what sends it back to read the config again. */
const copyOf = (instance: ReturnType<typeof makeI18n>) => Object.assign(
  Object.create(Object.getPrototypeOf(instance)),
  instance,
  { __original: instance },
);

let i18n = makeI18n();
const instanceAuth = vi.hoisted(() => ({
  remote: true,
  instanceKind: null as 'personal' | 'organization' | null,
  capabilities: {
    can_manage_instance: true,
    can_manage_access_members: true,
    can_chat: true,
    can_use_agents: true,
    can_use_skills: true,
    can_use_vault_secrets: true,
    can_use_files: true,
    can_use_terminal: true,
    can_use_terminal_files: true,
    can_use_system: true,
    can_manage_agents: true,
    can_manage_projects: true,
    can_read_instance: true,
    can_use_show_pages: true,
    is_instance_owner: true,
  },
}));

vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../context/StatusContext', () => ({ useStatus: () => ({ status }) }));
vi.mock('../context/WorkbenchInboxContext', () => ({ useWorkbenchInbox: () => inbox }));
vi.mock('../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => instanceAuth,
}));
vi.mock('../context/DockProvider', () => ({
  DockProvider: ({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) => (
    <div data-testid="dock-provider" data-enabled={String(enabled)}>{children}</div>
  ),
}));
// Captures the one prop the shell hands the manager: what to do when a window is
// about to come forward. The real manager calls it from `focus`/`openApp`; here
// the test calls it directly, which is the same event from the shell's side.
const windowManager = vi.hoisted(() => ({ foreground: null as (() => void) | null }));
vi.mock('../context/WindowManagerProvider', () => ({
  WindowManagerProvider: ({ children, onWindowForeground }: {
    children: ReactNode;
    onWindowForeground?: () => void;
  }) => {
    windowManager.foreground = onWindowForeground ?? null;
    return <>{children}</>;
  },
}));
vi.mock('../context/ShowPageDragProvider', () => ({
  ShowPageDragProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
// Stands down when its boundary suspends it, exactly as the real launcher does
// (`useRouteSurfaceActive() ? … : null`) — a stub that always rendered would
// report a live Apps button in every state the shell retires it.
vi.mock('./AppsLauncher', async () => {
  const { useRouteSurfaceActive } = await import('../lib/routeSurfaceActivity');
  return {
    AppsLauncher: () => (useRouteSurfaceActive() ? <div data-testid="apps-launcher" /> : null),
  };
});
vi.mock('./AccountMenu', () => ({ AccountMenu: () => <div data-testid="account-menu" /> }));
vi.mock('./LanguageSwitcher', () => ({ LanguageSwitcher: () => <div data-testid="language-switcher" /> }));
vi.mock('./ThemeToggle', () => ({ ThemeToggle: () => <div data-testid="theme-toggle" /> }));
vi.mock('./VersionBadge', () => ({ VersionBadge: () => null }));
vi.mock('./apps/MobileDockDrawer', () => ({
  MobileDockDrawer: () => <div data-testid="mobile-dock-drawer" />,
}));
vi.mock('./apps/WindowLayer', () => ({
  WindowLayer: ({ active }: { active: boolean }) => (
    <div data-testid="window-layer" data-active={String(active)} />
  ),
}));
vi.mock('./workbench/NewSessionSheet', () => ({
  NewSessionSheet: () => null,
}));
vi.mock('./workbench/WorkbenchSidebar', () => ({
  WorkbenchSidebar: () => <div data-testid="workbench-sidebar" />,
}));
vi.mock('./workbench/search/SearchPalette', () => ({
  SearchPalette: ({ open }: { open: boolean }) => <div data-testid="search-palette" data-open={String(open)} />,
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n,
  }),
}));

const SettingsExit = ({ testId }: { testId: string }) => {
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayOrigin(location);
  return <div data-testid={testId}>
    <button onClick={() => { if (origin) closeSettingsOverlay(navigate, origin); }}>settings.close</button>
  </div>;
};

// Stands in for the Settings surfaces, which read this and nothing else to
// decide whether their menu replaces the app sidebar or opens beside it.
const StandaloneMenuProbe = () => (
  <span data-testid="standalone-menu">{String(useStandaloneSettingsMenu())}</span>
);

beforeEach(() => {
  viewport.isDesktop = false;
  windowManager.foreground = null;
  window.localStorage.clear();
  clearMobileProjectsListSnapshot();
  instanceAuth.remote = true;
  instanceAuth.instanceKind = null;
  instanceAuth.capabilities.can_manage_instance = true;
  instanceAuth.capabilities.can_chat = true;
  instanceAuth.capabilities.can_use_show_pages = true;
  i18n = makeI18n();
  api.getConfig.mockResolvedValue({ platforms: { enabled: [] } });
  api.getMemorySettings.mockResolvedValue({
    status: 'failed',
    error: 'memory_settings_remote_only',
  });
  api.getVersion.mockResolvedValue({ version: 'test' });
});

afterEach(() => {
  cleanup();
  clearMobileProjectsListSnapshot();
  vi.clearAllMocks();
});

describe('AppShell setup access', () => {
  it.each([
    { remote: false, instanceKind: 'personal' as const },
    { remote: true, instanceKind: 'personal' as const },
    { remote: true, instanceKind: 'organization' as const },
  ])('renders the wizard for $instanceKind access (remote: $remote)', async (context) => {
    Object.assign(instanceAuth, context);
    render(
      <MemoryRouter initialEntries={['/setup']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/setup" element={<div data-testid="wizard">wizard</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('wizard')).toBeTruthy();
    expect(screen.queryByTestId('workbench-sidebar')).toBeNull();
  });
});

describe('AppShell search shortcut ownership', () => {
  it('yields a consumed search chord to the focused surface', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByTestId('workbench-surface');

    const consumed = new KeyboardEvent('keydown', {
      key: 'k',
      metaKey: true,
      bubbles: true,
      cancelable: true,
    });
    consumed.preventDefault();
    act(() => window.dispatchEvent(consumed));
    expect(screen.getByTestId('search-palette').getAttribute('data-open')).toBe('false');

    act(() => window.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'k',
      metaKey: true,
      bubbles: true,
      cancelable: true,
    })));
    expect(screen.getByTestId('search-palette').getAttribute('data-open')).toBe('true');
  });
});

describe('AppShell workbench sidebar', () => {
  // The sidebar's own container is hidden below md by CSS, which does not
  // unmount it. Its consumers fetch the inbox feed and the project tree on
  // mount, so a demand gate keyed on mounting is only true if mounting means
  // visible — the mount site owns that, not the sidebar's callers.
  it.each([
    [false, 0],
    [true, 1],
  ])('mounts only where it is visible (desktop: %s)', async (isDesktop, expectedMounts) => {
    viewport.isDesktop = isDesktop;

    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('workbench-surface')).toBeTruthy();
    expect(screen.queryAllByTestId('workbench-sidebar')).toHaveLength(expectedMounts);
    // The surrounding chrome is unaffected: only the data-reading member of the
    // desktop-only container is gated, not the container.
    expect(screen.getAllByText('appShell.title').length).toBeGreaterThan(0);
  });

  it('exposes the mobile scroll owner for page-level restoration', async () => {
    render(
      <MemoryRouter initialEntries={['/projects']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/projects" element={<div data-testid="projects-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('projects-surface')).toBeTruthy();
    expect(document.getElementById(APP_SHELL_SCROLL_ID)).not.toBeNull();
  });

  it('forgets the mobile projects list when leaving chat or projects', async () => {
    const user = userEvent.setup();
    holdMobileProjectsListForChatReturn({ visibleCounts: { proj_a: 16 }, scrollTop: 180 });

    const ChatProbe = () => {
      const navigate = useNavigate();
      return (
        <div data-testid="chat-surface">
          <button type="button" onClick={() => navigate('/inbox')}>
            leave-chat
          </button>
        </div>
      );
    };

    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/chat/:sessionId" element={<ChatProbe />} />
            <Route path="/inbox" element={<div data-testid="inbox-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('chat-surface')).toBeTruthy();
    expect(readMobileProjectsListSnapshot()).toEqual({
      visibleCounts: { proj_a: 16 },
      scrollTop: 180,
    });

    await user.click(screen.getByRole('button', { name: 'leave-chat' }));
    expect(await screen.findByTestId('inbox-surface')).toBeTruthy();
    expect(readMobileProjectsListSnapshot()).toEqual({ visibleCounts: {}, scrollTop: 0 });
  });
});

describe('AppShell sidebar width', () => {
  const renderShell = (initialEntry = '/') => render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="*" element={<div data-testid="surface" />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

  // The sidebar and the content offset must read ONE value, or dragging the
  // divider would move the sidebar out from under the page it frames.
  it('sizes the sidebar and offsets the content from the same width', async () => {
    viewport.isDesktop = true;
    renderShell();
    await screen.findByTestId('surface');

    expect(document.querySelector('aside')?.className).toContain('w-[var(--app-sidebar-w)]');
    expect(document.getElementById(APP_SHELL_SCROLL_ID)?.className)
      .toContain('md:ml-[var(--app-sidebar-w)]');
  });

  it.each([
    [true, 1],
    [false, 0],
  ])('offers the resize separator only in desktop layout (desktop: %s)', async (isDesktop, expected) => {
    viewport.isDesktop = isDesktop;
    renderShell();
    await screen.findByTestId('surface');

    expect(screen.queryAllByRole('separator', { name: 'appShell.resizeSidebar' }))
      .toHaveLength(expected);
  });

  // Standalone Settings stands IN FOR this column, so the two have to agree on
  // one width or the left edge jumps as Settings opens. Inline Settings opens
  // beside the column, which therefore has to stay live — navigable, keyboard
  // reachable, and able to raise the palettes, which float above the surface.
  it.each([
    ['standalone', true],
    ['inline', false],
  ] as const)('retires the sidebar only where Settings replaces it (%s)', async (
    placement,
    covered,
  ) => {
    viewport.isDesktop = true;
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, placement);
    renderShell('/settings/general');
    await screen.findByTestId('surface');

    const aside = document.querySelector('aside');
    expect(aside?.hasAttribute('inert')).toBe(covered);
    expect(aside?.className.includes('invisible')).toBe(covered);
    expect(document.getElementById(APP_SHELL_SCROLL_ID)?.className
      .includes('md:ml-[var(--app-sidebar-w)]')).toBe(!covered);

    // ⌘K belongs to whichever surface owns the shell.
    act(() => window.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'k',
      metaKey: true,
      bubbles: true,
      cancelable: true,
    })));
    expect(screen.getByTestId('search-palette').getAttribute('data-open')).toBe(String(!covered));
  });

  // The exception to the rule above, and the reason the two flags exist at all.
  // The window layer spans the whole viewport at z-20 so a window can be dragged
  // over the sidebar; inline Settings is opaque from the sidebar's trailing edge
  // at z-30. Left live, a window would show as a strip over the very column
  // inline exists to keep, with everything that makes it a window — title bar,
  // controls, content — behind Settings. Live-but-invisible is worse than
  // retired, so this one retires in both placements.
  it.each(['standalone', 'inline'] as const)(
    'retires the window layer under %s Settings',
    async (placement) => {
      viewport.isDesktop = true;
      window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, placement);
      renderShell('/settings/general');
      await screen.findByTestId('surface');

      expect(screen.getByTestId('window-layer').parentElement?.hasAttribute('hidden')).toBe(true);
      expect(screen.getByTestId('window-layer').getAttribute('data-active')).toBe('false');
    },
  );

  // ...and comes back, so retiring it is not a way of losing it.
  it('restores the window layer once Settings closes', async () => {
    viewport.isDesktop = true;
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, 'inline');
    renderShell('/');
    await screen.findByTestId('surface');

    expect(screen.getByTestId('window-layer').parentElement?.hasAttribute('hidden')).toBe(false);
    expect(screen.getByTestId('window-layer').getAttribute('data-active')).toBe('true');
    expect(screen.getByTestId('apps-launcher')).toBeTruthy();
  });

  // The launcher does NOT go with the layer. It is a sidebar control, and it
  // retires with the column it sits in like every other control there: gone
  // under standalone, which stands in for that column, present under inline,
  // which keeps it. Taking Apps away under inline is the sidebar losing a button
  // that is on screen the rest of the time, for no reason the user can see.
  it.each([
    ['standalone', false],
    ['inline', true],
  ] as const)('keeps the Apps launcher wherever the sidebar stays live (%s)', async (
    placement,
    live,
  ) => {
    viewport.isDesktop = true;
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, placement);
    renderShell('/settings/general');
    await screen.findByTestId('surface');

    expect(Boolean(screen.queryByTestId('apps-launcher'))).toBe(live);
  });

  // What makes keeping it safe: the layer a window arrives in is hidden while
  // Settings is open, so coming forward has to leave Settings first — the same
  // exit a sidebar link takes by navigating, and the toggle beside it by closing.
  // The shell says it once, to the manager, rather than each caller working out
  // what is covering the layer.
  it('leaves Settings when a window comes forward', async () => {
    viewport.isDesktop = true;
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, 'inline');
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={['/chat/session-1']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="chat/:sessionId" element={<div data-testid="chat" />} />
            <Route path="settings/general" element={<SettingsExit testId="settings" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('chat')).toBeTruthy();
    await user.click(screen.getByRole('link', { name: 'appShell.openControlPanel' }));
    expect(await screen.findByTestId('settings')).toBeTruthy();
    expect(screen.getByTestId('window-layer').parentElement?.hasAttribute('hidden')).toBe(true);

    act(() => windowManager.foreground?.());

    expect(await screen.findByTestId('chat')).toBeTruthy();
    expect(screen.getByTestId('window-layer').parentElement?.hasAttribute('hidden')).toBe(false);
  });

  // The window it opens is the foreground, not a reason to leave one route for
  // another: with Settings closed there is nothing to clear out of the way.
  it('stays put when a window comes forward outside Settings', async () => {
    viewport.isDesktop = true;
    render(
      <MemoryRouter initialEntries={['/chat/session-1']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench" />} />
            <Route path="chat/:sessionId" element={<div data-testid="chat" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByTestId('chat')).toBeTruthy();

    act(() => windowManager.foreground?.());

    expect(screen.getByTestId('chat')).toBeTruthy();
    expect(screen.queryByTestId('workbench')).toBeNull();
  });

  it('covers the shell below md even when inline is the stored preference', async () => {
    viewport.isDesktop = false;
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, 'inline');
    renderShell('/settings/general');
    await screen.findByTestId('surface');

    // A phone has no room for two rails, so the preference is not in force.
    expect(document.querySelector('aside')?.hasAttribute('inert')).toBe(true);
    expect(document.getElementById(APP_SHELL_SCROLL_ID)?.className)
      .not.toContain('md:ml-[var(--app-sidebar-w)]');
  });

  it('leaves a standalone app tab without a sidebar to resize', async () => {
    viewport.isDesktop = true;
    // The shell reads standalone mode from the document URL, once, at mount.
    window.history.replaceState({}, '', `/apps/editor?${APP_TAB_PARAM}=1`);
    try {
      renderShell('/apps/editor');
      await screen.findByTestId('surface');

      expect(document.querySelector('aside')).toBeNull();
      expect(screen.queryByRole('separator', { name: 'appShell.resizeSidebar' })).toBeNull();
    } finally {
      window.history.replaceState({}, '', '/');
    }
  });

  // The same tab, asked the question Settings actually asks. `inline` is a claim
  // about the app sidebar being on screen to open beside; here there is none, so
  // Settings has to come up standalone however the owner set the preference for
  // the windows that do have one. The shell states that, because only the shell
  // can: standalone mode comes from a document flag frozen at mount, which no
  // pathname predicate can recover — and the route Settings lands on says
  // nothing about the shell it opened over anyway, as with the config-recovery
  // banner's Diagnostics link in a single-app tab.
  it('publishes a sidebar-free shell to the Settings surfaces above it', async () => {
    viewport.isDesktop = true;
    window.localStorage.setItem(SETTINGS_MENU_PLACEMENT_STORAGE_KEY, 'inline');
    window.history.replaceState({}, '', `/apps/editor?${APP_TAB_PARAM}=1`);
    try {
      render(
        <MemoryRouter initialEntries={['/apps/editor']}>
          <Routes>
            <Route element={<AppShell />}>
              <Route path="*" element={<StandaloneMenuProbe />} />
            </Route>
          </Routes>
        </MemoryRouter>,
      );

      expect((await screen.findByTestId('standalone-menu')).textContent).toBe('true');
    } finally {
      window.history.replaceState({}, '', '/');
    }
  });
});

describe('AppShell persistent Workbench chrome', () => {
  it('applies the instance language while Settings controls stay unmounted', async () => {
    viewport.isDesktop = true;
    api.getConfig.mockResolvedValue({ language: 'zh', platforms: { enabled: [] } });

    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('workbench')).toBeTruthy();
    expect(screen.queryByTestId('language-switcher')).toBeNull();
    await waitFor(() => expect(i18n.changeLanguage).toHaveBeenCalledWith('zh'));
  });

  it('keeps a language the user picked while the instance config was still being read', async () => {
    viewport.isDesktop = true;
    const instance = i18n;
    let answer: (config: unknown) => void = () => {};
    api.getConfig.mockReturnValue(new Promise((resolve) => { answer = resolve; }));

    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByTestId('workbench')).toBeTruthy();

    // They pick before the read answers, from a control the shell never sees.
    await act(async () => { await selectLanguage(instance, 'zh'); });
    await act(async () => { answer({ language: 'en', platforms: { enabled: [] } }); });

    // A read that started before the pick cannot be the answer to it.
    expect(instance.changeLanguage.mock.calls.map(([code]) => code)).toEqual(['zh']);
    expect(instance.language).toBe('zh');
  });

  it('does not undo that pick when the language change itself starts the read again', async () => {
    viewport.isDesktop = true;
    const instance = i18n;
    // The instance config as it was before the pick — what a reader that cached
    // it, or one racing the save, still has to hand back.
    api.getConfig.mockResolvedValue({ language: 'en', platforms: { enabled: [] } });

    // A function, not one element: React skips a re-render handed back the very
    // element it already rendered, and the shell re-rendering is the point here.
    const tree = () => (
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench" />} />
          </Route>
        </Routes>
      </MemoryRouter>
    );
    const view = render(tree());
    expect(await screen.findByTestId('workbench')).toBeTruthy();
    await waitFor(() => expect(api.getConfig).toHaveBeenCalledTimes(1));

    await act(async () => { await selectLanguage(instance, 'zh'); });

    // The language change replaces the object every consumer holds, which is
    // what sends the shell back for a second read of the older config.
    i18n = copyOf(instance);
    await act(async () => { view.rerender(tree()); });

    await waitFor(() => expect(api.getConfig).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(instance.changeLanguage.mock.calls.map(([code]) => code)).toEqual(['zh']);
    expect(instance.language).toBe('zh');
  });

  it.each([
    'personal',
    null,
    'organization',
  ] as const)('keeps Settings in the Workbench sidebar without preference controls for %s instances', async (instanceKind) => {
    viewport.isDesktop = true;
    instanceAuth.instanceKind = instanceKind;

    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('workbench')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'appShell.openControlPanel' }).getAttribute('href')).toBe(
      '/settings/general',
    );
    expect(screen.queryByTestId('language-switcher')).toBeNull();
    expect(screen.queryByTestId('theme-toggle')).toBeNull();
    expect(screen.queryByTestId('account-menu')).toBeNull();
  });

  it('uses the Settings button to return to the route that opened Settings', async () => {
    viewport.isDesktop = true;
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={['/chat/session-1']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench" />} />
            <Route path="chat/:sessionId" element={<div data-testid="chat" />} />
            <Route path="settings/general" element={<SettingsExit testId="settings" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('chat')).toBeTruthy();
    const settingsToggle = screen.getByRole('link', { name: 'appShell.openControlPanel' });
    expect(settingsToggle.getAttribute('href')).toBe('/settings/general');
    await user.click(settingsToggle);

    expect(await screen.findByTestId('settings')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'settings.close' }));
    expect(await screen.findByTestId('chat')).toBeTruthy();
  });

  it('returns to chat when a recovery action opens Settings without route state', async () => {
    viewport.isDesktop = true;
    api.getConfig.mockResolvedValue({
      config_recovery: { required: true, warnings: ['invalid-config'] },
    });
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={['/chat/session-1']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="chat/:sessionId" element={<div data-testid="chat" />} />
            <Route path="settings/diagnostics" element={<SettingsExit testId="diagnostics" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole('link', { name: 'configRecovery.action' }));
    expect(await screen.findByTestId('diagnostics')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'settings.close' }));
    expect(await screen.findByTestId('chat')).toBeTruthy();
  });
});

describe('AppShell remote Apps access', () => {
  it('gives the mobile Show Page app the full viewport without duplicate shell chrome', async () => {
    render(
      <MemoryRouter initialEntries={['/apps/show/session-1']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="apps/show/:sessionId" element={<div data-testid="show-page-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const surface = await screen.findByTestId('show-page-surface');
    expect(screen.queryByRole('banner')).toBeNull();
    expect(document.querySelector('nav.fixed.inset-x-0.bottom-0')).toBeNull();
    expect(surface.parentElement?.className).toContain('h-full p-0');
    expect(surface.parentElement?.className).not.toContain('px-4 py-5');
  });

  it.each(['/apps/files', '/apps/terminal', '/apps/editor'])('gives the mobile built-in app %s the full viewport', async (path) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path={path.slice(1)} element={<div data-testid="builtin-app-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const surface = await screen.findByTestId('builtin-app-surface');
    expect(screen.queryByRole('banner')).toBeNull();
    expect(document.querySelector('nav.fixed.inset-x-0.bottom-0')).toBeNull();
    expect(surface.parentElement?.className).toContain('h-full p-0');
    expect(surface.parentElement?.className).not.toContain('px-4 py-5');
  });

  it.each([
    ['owner', true],
    ['member', false],
  ])('mounts the Apps shell for an authenticated remote %s', async (_role, canManageInstance) => {
    instanceAuth.capabilities.can_manage_instance = canManageInstance;
    instanceAuth.capabilities.can_chat = true;

    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<div data-testid="workbench-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('workbench-surface')).toBeTruthy();
    expect(screen.getByTestId('dock-provider')).toBeTruthy();
    expect(screen.getByTestId('apps-launcher')).toBeTruthy();
    expect(screen.getByTestId('mobile-dock-drawer')).toBeTruthy();
    expect(screen.getByTestId('window-layer')).toBeTruthy();
  });

  it('hides Apps surfaces and redirects App routes for non-Organization remote users', async () => {
    instanceAuth.capabilities.can_chat = false;

    render(
      <MemoryRouter initialEntries={['/apps/library']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="apps/library" element={<div data-testid="library-surface" />} />
            <Route index element={<div data-testid="workbench-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('workbench-surface')).toBeTruthy();
    expect(screen.queryByTestId('library-surface')).toBeNull();
    expect(screen.queryByTestId('apps-launcher')).toBeNull();
    expect(screen.queryByTestId('mobile-dock-drawer')).toBeNull();
    expect(screen.queryByTestId('window-layer')).toBeNull();
    expect(screen.getByTestId('dock-provider').getAttribute('data-enabled')).toBe('false');
  });

  it.each([
    ['owner', true],
    ['member', false],
  ])('keeps App Library available to a remote %s', async (_role, canManageInstance) => {
    instanceAuth.capabilities.can_manage_instance = canManageInstance;
    instanceAuth.capabilities.can_chat = true;

    render(
      <MemoryRouter initialEntries={['/apps/library']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="apps/library" element={<div data-testid="library-surface" />} />
            <Route index element={<div data-testid="redirected-workbench" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('library-surface')).toBeTruthy();
    expect(screen.queryByTestId('redirected-workbench')).toBeNull();
  });

  it.each([
    '/apps/files',
    '/apps/editor',
    '/apps/terminal',
    '/apps/library',
    '/apps/show/session-1',
  ])('keeps the remote App route %s available when legacy capabilities are false', async (path) => {
    instanceAuth.capabilities.can_manage_instance = false;
    instanceAuth.capabilities.can_chat = true;

    render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path={path.slice(1)} element={<div data-testid="app-surface" />} />
            <Route index element={<div data-testid="redirected-workbench" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('app-surface')).toBeTruthy();
    expect(screen.queryByTestId('redirected-workbench')).toBeNull();
  });

  it('lets a Viewer open an authorized Show Page app without the Editor Apps capability', async () => {
    instanceAuth.capabilities.can_manage_instance = false;
    instanceAuth.capabilities.can_chat = false;
    instanceAuth.capabilities.can_use_show_pages = true;

    render(
      <MemoryRouter initialEntries={['/apps/show/session-1']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="apps/show/:sessionId" element={<div data-testid="show-page-surface" />} />
            <Route index element={<div data-testid="redirected-workbench" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('show-page-surface')).toBeTruthy();
    expect(screen.queryByTestId('redirected-workbench')).toBeNull();
  });

  it('still withholds Editor-only Apps from a Viewer', async () => {
    instanceAuth.capabilities.can_manage_instance = false;
    instanceAuth.capabilities.can_chat = false;
    instanceAuth.capabilities.can_use_show_pages = true;

    render(
      <MemoryRouter initialEntries={['/apps/library']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="apps/library" element={<div data-testid="library-surface" />} />
            <Route index element={<div data-testid="workbench-surface" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('workbench-surface')).toBeTruthy();
    expect(screen.queryByTestId('library-surface')).toBeNull();
  });
});
