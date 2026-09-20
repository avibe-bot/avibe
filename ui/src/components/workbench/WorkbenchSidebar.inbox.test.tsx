/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RouteSurfaceActivityBoundary } from '../RouteSurfaceActivityBoundary';
import { tabModifierLabel } from '../../apps/appLaunch';

const inbox = vi.hoisted(() => ({
  totalUnread: 0,
  unreadSessions: 0,
  inboxSessions: [] as unknown[],
  unreadBySession: {} as Record<string, number>,
  markRead: vi.fn(),
}));
const hookCalls = vi.hoisted(() => ({
  inbox: vi.fn(),
  tree: vi.fn(),
}));
const capabilities = vi.hoisted(() => ({
  can_chat: true,
  can_manage_projects: true,
  can_use_agents: true,
  can_use_skills: true,
  can_use_vault_secrets: true,
}));

vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: (options?: { feed?: boolean }) => {
    hookCalls.inbox(options);
    return inbox;
  },
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities }),
}));
vi.mock('../../context/WorkbenchProjectsContext', () => ({
  useWorkbenchProjectsTree: (options?: { active?: boolean }) => {
    hookCalls.tree(options);
    return {
      projects: [],
      projectsError: null,
      sessionsOf: () => [],
      isExpanded: () => false,
      toggleExpanded: vi.fn(),
      loadMore: vi.fn(),
      creatingSession: null,
      createSessionForProject: vi.fn(),
      renameProject: vi.fn(),
      archiveProject: vi.fn(),
      reorderProjects: vi.fn(),
      isReorderingProjects: false,
    };
  },
  useWorkbenchProjectsActions: () => ({}),
}));
vi.mock('../../context/WindowManagerContext', () => ({ useWindowManager: () => ({ open: vi.fn() }) }));
vi.mock('../../context/useUnsavedChangesActionGuard', () => ({
  useUnsavedChangesActionGuard: () => (run: () => void) => run,
}));

import en from '../../i18n/en.json';
import { WorkbenchSidebar } from './WorkbenchSidebar';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const sidebarElement = (active = true, onOpenSearch?: () => void) => (
  <I18nextProvider i18n={i18n}>
    <MemoryRouter initialEntries={['/']}>
      <RouteSurfaceActivityBoundary active={active}>
        <WorkbenchSidebar onOpenSearch={onOpenSearch} />
      </RouteSurfaceActivityBoundary>
    </MemoryRouter>
  </I18nextProvider>
);
const renderSidebar = (active = true, onOpenSearch?: () => void) => render(sidebarElement(active, onOpenSearch));

const inboxIcon = () => screen.getByRole('link', { name: en.workbench.nav.inbox }).querySelector('svg')!;

beforeEach(() => {
  inbox.totalUnread = 0;
  inbox.unreadSessions = 0;
  inbox.inboxSessions = [];
  inbox.unreadBySession = {};
  hookCalls.inbox.mockClear();
  hookCalls.tree.mockClear();
  Object.assign(capabilities, {
    can_chat: true,
    can_manage_projects: true,
    can_use_agents: true,
    can_use_skills: true,
    can_use_vault_secrets: true,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Workbench sidebar inbox counter', () => {
  it('says nothing at all when nothing is unread', () => {
    renderSidebar();

    const link = screen.getByRole('link', { name: en.workbench.nav.inbox });
    // A zero badge is still a badge — an empty inbox draws no attention at all.
    expect(link.textContent).toBe(en.workbench.nav.inbox);
    expect(inboxIcon().getAttribute('class')).toContain('text-muted');
    expect(inboxIcon().getAttribute('class')).not.toContain('text-cyan-ink');
  });

  it('shows the real unread count and colours the icon with it', () => {
    inbox.totalUnread = 7;
    inbox.unreadSessions = 3;
    inbox.unreadBySession = { ses_a: 4, ses_b: 3 };
    renderSidebar();

    expect(screen.getByRole('link', { name: en.workbench.nav.inbox }).textContent).toBe(`${en.workbench.nav.inbox}7`);
    expect(inboxIcon().getAttribute('class')).toContain('text-cyan-ink');
  });

  it('caps the badge instead of widening the icon', () => {
    inbox.totalUnread = 150;
    renderSidebar();

    expect(screen.getByRole('link', { name: en.workbench.nav.inbox }).textContent).toBe(`${en.workbench.nav.inbox}99+`);
  });

  it('shows the platform-appropriate search shortcut', () => {
    renderSidebar();

    expect(screen.getByText(`${tabModifierLabel()}K`)).toBeTruthy();
  });

  it('withdraws feed and tree activation while the retained sidebar is hidden', () => {
    renderSidebar(false);

    expect(hookCalls.inbox).toHaveBeenCalledWith({ feed: false });
    expect(hookCalls.tree).toHaveBeenCalledWith({ active: false });
  });
});

describe('Workbench sidebar capability navigation', () => {
  it('can collapse and restore the capability links', async () => {
    const user = userEvent.setup();
    const onOpenSearch = vi.fn();
    renderSidebar(true, onOpenSearch);
    const destinations = [
      en.workbench.nav.agents,
      en.workbench.nav.skills,
      en.workbench.nav.harness,
      en.workbench.nav.vaults,
    ];

    const toggle = screen.getByRole('button', { name: en.nav.capabilities });
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('link', { name: `${en.appShell.title} Agent OS` }).getAttribute('href')).toBe('/');
    expect(screen.getByRole('navigation').id).toBe(toggle.getAttribute('aria-controls'));
    for (const name of destinations) expect(screen.getByRole('link', { name })).toBeTruthy();

    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    for (const name of destinations) expect(screen.queryByRole('link', { name })).toBeNull();
    expect(screen.getByRole('link', { name: en.workbench.nav.inbox })).toBeTruthy();
    expect(screen.getByText(en.workbench.projectsLabel)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: en.workbench.search.entry }));
    expect(onOpenSearch).toHaveBeenCalledOnce();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');

    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    for (const name of destinations) expect(screen.getByRole('link', { name })).toBeTruthy();
  });

  it('preserves collapsed navigation across Settings suspension and reactivation', async () => {
    const user = userEvent.setup();
    const { rerender } = renderSidebar();
    await user.click(screen.getByRole('button', { name: en.nav.capabilities }));

    rerender(sidebarElement(false));
    expect(hookCalls.inbox).toHaveBeenLastCalledWith({ feed: false });
    expect(hookCalls.tree).toHaveBeenLastCalledWith({ active: false });

    rerender(sidebarElement(true));
    expect(hookCalls.inbox).toHaveBeenLastCalledWith({ feed: true });
    expect(hookCalls.tree).toHaveBeenLastCalledWith({ active: true });
    expect(screen.getByRole('button', { name: en.nav.capabilities }).getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('link', { name: en.workbench.nav.agents })).toBeNull();
  });

  it('only restores destinations permitted by the current authorization', async () => {
    const user = userEvent.setup();
    capabilities.can_chat = false;
    capabilities.can_use_skills = false;
    capabilities.can_use_vault_secrets = false;
    renderSidebar();

    const toggle = screen.getByRole('button', { name: en.nav.capabilities });
    await user.click(toggle);
    await user.click(toggle);

    expect(screen.getByRole('link', { name: en.workbench.nav.agents })).toBeTruthy();
    expect(screen.queryByRole('link', { name: en.workbench.nav.skills })).toBeNull();
    expect(screen.queryByRole('link', { name: en.workbench.nav.harness })).toBeNull();
    expect(screen.queryByRole('link', { name: en.workbench.nav.vaults })).toBeNull();
  });

  it('omits the capability group when no destinations are permitted', () => {
    capabilities.can_chat = false;
    capabilities.can_use_agents = false;
    capabilities.can_use_skills = false;
    capabilities.can_use_vault_secrets = false;
    renderSidebar();

    expect(screen.queryByRole('button', { name: en.nav.capabilities })).toBeNull();
    expect(screen.queryByRole('navigation')).toBeNull();
  });
});
