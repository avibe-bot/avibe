/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RouteSurfaceActivityBoundary } from '../RouteSurfaceActivityBoundary';

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
  // Project creation is `can_manage_projects && can_use_files`; both are named so
  // a case can withhold creation alone and still watch Search stay reachable.
  can_use_files: true,
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
// The dialog's own flow is covered elsewhere; here only its completion matters.
vi.mock('./NewProjectDialog', () => ({
  NewProjectDialog: ({ onCreated }: { onCreated: (project: { id: string }) => void }) => (
    <button type="button" onClick={() => onCreated({ id: 'proj_new' })}>
      finish new project
    </button>
  ),
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

const inboxIcon = () => screen.getByRole('link', { name: (name) => name.startsWith(en.workbench.nav.inbox) }).querySelector('svg')!;
const projectsToggle = () => screen.getByRole('button', { name: en.workbench.projectsLabel });

beforeEach(() => {
  // Section open state is remembered per browser; every case starts from a fresh one.
  window.localStorage.clear();
  inbox.totalUnread = 0;
  inbox.unreadSessions = 0;
  inbox.inboxSessions = [];
  inbox.unreadBySession = {};
  hookCalls.inbox.mockClear();
  hookCalls.tree.mockClear();
  Object.assign(capabilities, {
    can_chat: true,
    can_manage_projects: true,
    can_use_files: true,
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
    // The entry is the brand row's icon (docs/plans/bots.md D5), so it holds no text.
    expect(link.textContent).toBe('');
    // Cyan is the unread read; the resting row is plain foreground ink.
    expect(inboxIcon().getAttribute('class')).toContain('text-foreground');
    expect(inboxIcon().getAttribute('class')).not.toContain('text-cyan-ink');
  });

  it('shows the real unread count and colours the icon with it', () => {
    inbox.totalUnread = 7;
    inbox.unreadSessions = 3;
    inbox.unreadBySession = { ses_a: 4, ses_b: 3 };
    renderSidebar();

    const link = screen.getByRole('link', { name: en.workbench.inbox.navUnread.replace('{{count}}', '7') });
    expect(link.textContent).toBe('7');
    expect(inboxIcon().getAttribute('class')).toContain('text-cyan-ink');
  });

  it('caps the badge instead of widening the icon', () => {
    inbox.totalUnread = 150;
    renderSidebar();

    expect(screen.getByRole('link', { name: en.workbench.inbox.navUnread.replace('{{count}}', '150') }).textContent).toBe('99+');
  });

  it('keeps Search in the Projects header, before New Project and without needing it', async () => {
    const user = userEvent.setup();
    const onOpenSearch = vi.fn();
    const { rerender } = renderSidebar(true, onOpenSearch);

    const header = projectsToggle().parentElement!;
    expect(within(header).getAllByRole('button').map((button) => button.getAttribute('aria-label') ?? button.textContent))
      .toEqual([en.workbench.projectsLabel, en.workbench.search.entry, en.workbench.addProject]);

    // Search is not a project-creation affordance: withholding creation must
    // take the add control away and leave the palette entry exactly where it is.
    capabilities.can_use_files = false;
    rerender(sidebarElement(true, onOpenSearch));
    expect(screen.queryByRole('button', { name: en.workbench.addProject })).toBeNull();
    await user.click(within(header).getByRole('button', { name: en.workbench.search.entry }));
    expect(onOpenSearch).toHaveBeenCalledOnce();
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
    expect(screen.getByRole('link', { name: `${en.appShell.title} ${en.appShell.subtitle}` }).getAttribute('href')).toBe('/');
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

describe('Workbench sidebar projects section', () => {
  it('collapses the project list and keeps Search and New project in its header', async () => {
    const user = userEvent.setup();
    const onOpenSearch = vi.fn();
    const { container } = renderSidebar(true, onOpenSearch);

    expect(projectsToggle().getAttribute('aria-expanded')).toBe('true');
    expect(container.querySelector(`#${projectsToggle().getAttribute('aria-controls')}`)).toBeTruthy();
    expect(screen.getByText(en.workbench.projectsEmpty)).toBeTruthy();

    await user.click(projectsToggle());
    expect(projectsToggle().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText(en.workbench.projectsEmpty)).toBeNull();
    expect(screen.getByRole('button', { name: en.workbench.addProject })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: en.workbench.search.entry }));
    expect(onOpenSearch).toHaveBeenCalledOnce();

    // The chevron closing the row is a second pointer target for the same toggle.
    const chevron = projectsToggle().parentElement!.lastElementChild as HTMLElement;
    await user.click(chevron);
    expect(projectsToggle().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText(en.workbench.projectsEmpty)).toBeTruthy();
  });

  it('remembers both collapsed groups across a reload', async () => {
    const user = userEvent.setup();
    const first = renderSidebar();
    await user.click(screen.getByRole('button', { name: en.nav.capabilities }));
    await user.click(projectsToggle());
    first.unmount();

    renderSidebar();
    expect(screen.getByRole('button', { name: en.nav.capabilities }).getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('link', { name: en.workbench.nav.agents })).toBeNull();
    expect(projectsToggle().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText(en.workbench.projectsEmpty)).toBeNull();
  });

  it('opens the list when a project is created while it is collapsed', async () => {
    const user = userEvent.setup();
    renderSidebar();
    await user.click(projectsToggle());

    await user.click(screen.getByRole('button', { name: en.workbench.addProject }));
    await user.click(screen.getByRole('button', { name: 'finish new project' }));

    expect(projectsToggle().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText(en.workbench.projectsEmpty)).toBeTruthy();
  });
});
