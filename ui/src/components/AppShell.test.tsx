/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import type { ReactNode } from 'react';

import {
  APP_SHELL_SCROLL_ID,
  clearMobileProjectsListSnapshot,
  holdMobileProjectsListForChatReturn,
  readMobileProjectsListSnapshot,
} from '../lib/mobileProjectsListMemory';
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
vi.mock('../context/WindowManagerProvider', () => ({
  WindowManagerProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock('../context/ShowPageDragProvider', () => ({
  ShowPageDragProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock('./AppsLauncher', () => ({ AppsLauncher: () => <div data-testid="apps-launcher" /> }));
vi.mock('./AccountMenu', () => ({ AccountMenu: () => null }));
vi.mock('./LanguageSwitcher', () => ({ LanguageSwitcher: () => null }));
vi.mock('./ThemeToggle', () => ({ ThemeToggle: () => null }));
vi.mock('./VersionBadge', () => ({ VersionBadge: () => null }));
vi.mock('./apps/MobileDockDrawer', () => ({
  MobileDockDrawer: () => <div data-testid="mobile-dock-drawer" />,
}));
vi.mock('./apps/WindowLayer', () => ({ WindowLayer: () => <div data-testid="window-layer" /> }));
vi.mock('./workbench/NewSessionSheet', () => ({
  NewSessionSheet: () => null,
}));
vi.mock('./workbench/WorkbenchSidebar', () => ({
  WorkbenchSidebar: () => <div data-testid="workbench-sidebar" />,
}));
vi.mock('./workbench/search/SearchPalette', () => ({ SearchPalette: () => null }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: {
      language: 'en',
      options: { resources: { en: {}, zh: {} } },
      changeLanguage: vi.fn(),
    },
  }),
}));

beforeEach(() => {
  viewport.isDesktop = false;
  clearMobileProjectsListSnapshot();
  instanceAuth.instanceKind = null;
  instanceAuth.capabilities.can_manage_instance = true;
  instanceAuth.capabilities.can_chat = true;
  instanceAuth.capabilities.can_use_show_pages = true;
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

describe('AppShell setup recovery', () => {
  it('shows the remote-owner recovery card instead of the wizard', async () => {
    render(
      <MemoryRouter initialEntries={['/setup']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/setup" element={<div data-testid="wizard">wizard</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText('setup.remoteOwner.title')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'setup.remoteOwner.action' }).getAttribute('href')).toBe('/admin/dashboard');
    expect(screen.queryByTestId('wizard')).toBeNull();
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

describe('AppShell Permissions navigation', () => {
  it.each([
    'personal',
    null,
    'organization',
  ] as const)('keeps current-instance Permissions in More Settings for %s instances', async (instanceKind) => {
    const user = userEvent.setup();
    instanceAuth.instanceKind = instanceKind;

    render(
      <MemoryRouter initialEntries={['/admin/dashboard']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="admin/dashboard" element={<div data-testid="dashboard" />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId('dashboard')).toBeTruthy();

    const moreTab = screen.getByRole('button', { name: 'nav.more' });
    const bottomNav = moreTab.closest('nav');
    expect(bottomNav).not.toBeNull();
    expect(within(bottomNav!).queryByText('nav.permissions')).toBeNull();

    await user.click(moreTab);
    const moreSettings = screen.getByRole('dialog');
    expect(within(moreSettings).getByRole('link', { name: 'nav.permissions' }).getAttribute('href')).toBe(
      '/admin/permissions',
    );

    const desktopSidebar = document.querySelector('aside');
    expect(desktopSidebar).not.toBeNull();
    expect(within(desktopSidebar!).getByRole('link', { name: 'nav.permissions' }).getAttribute('href')).toBe(
      '/admin/permissions',
    );
  });
});

describe('AppShell remote Apps access', () => {
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
