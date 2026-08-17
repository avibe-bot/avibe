/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ReactNode } from 'react';

import { AppShell } from './AppShell';

vi.hoisted(() => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
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
vi.mock('./workbench/WorkbenchSidebar', () => ({ WorkbenchSidebar: () => <div /> }));
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

describe('AppShell Permissions navigation', () => {
  it.each([
    ['personal', 2],
    [null, 2],
    ['organization', 2],
  ] as const)('shows current-instance Permissions for %s instances', async (instanceKind, expectedCount) => {
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
    const permissionEntries = screen.queryAllByText('nav.permissions');
    expect(permissionEntries).toHaveLength(expectedCount);
    const advancedEntries = screen.queryAllByText('nav.advancedSettings');
    expect(advancedEntries).toHaveLength(expectedCount);
    permissionEntries.forEach((permission, index) => {
      expect(
        permission.compareDocumentPosition(advancedEntries[index]!)
          & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    });
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
