/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
const auth = vi.hoisted(() => ({
  capabilities: {
    can_chat: true,
    can_manage_projects: true,
    can_use_files: true,
    can_use_agents: true,
    can_use_skills: true,
    can_use_vault_secrets: true,
  } as Record<string, boolean>,
}));
const hookCalls = vi.hoisted(() => ({
  inbox: vi.fn(),
  tree: vi.fn(),
}));

vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: (options?: { feed?: boolean }) => {
    hookCalls.inbox(options);
    return inbox;
  },
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => auth,
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

const renderSidebar = (
  { active = true, onOpenSearch }: { active?: boolean; onOpenSearch?: () => void } = {},
) => render(
  <I18nextProvider i18n={i18n}>
    <MemoryRouter initialEntries={['/']}>
      <RouteSurfaceActivityBoundary active={active}>
        <WorkbenchSidebar onOpenSearch={onOpenSearch} />
      </RouteSurfaceActivityBoundary>
    </MemoryRouter>
  </I18nextProvider>,
);

const inboxIcon = () => screen.getByRole('link', { name: en.workbench.nav.inbox }).querySelector('svg')!;
const capabilityToggle = () => screen.getByRole('button', { name: en.nav.capabilities });
const capabilityHrefs = () =>
  [en.workbench.nav.agents, en.workbench.nav.skills, en.workbench.nav.harness, en.workbench.nav.vaults]
    .map((name) => screen.queryByRole('link', { name })?.getAttribute('href') ?? null);

beforeEach(() => {
  inbox.totalUnread = 0;
  inbox.unreadSessions = 0;
  inbox.inboxSessions = [];
  inbox.unreadBySession = {};
  auth.capabilities = {
    can_chat: true,
    can_manage_projects: true,
    can_use_files: true,
    can_use_agents: true,
    can_use_skills: true,
    can_use_vault_secrets: true,
  };
  window.localStorage.clear();
  hookCalls.inbox.mockClear();
  hookCalls.tree.mockClear();
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
    // Cyan is the unread read; the resting row is plain foreground ink.
    expect(inboxIcon().getAttribute('class')).toContain('text-foreground');
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

  it('withdraws feed and tree activation while the retained sidebar is hidden', () => {
    renderSidebar({ active: false });

    expect(hookCalls.inbox).toHaveBeenCalledWith({ feed: false });
    expect(hookCalls.tree).toHaveBeenCalledWith({ active: false });
  });
});

describe('Workbench sidebar capabilities section', () => {
  it('lists every capability row the authorization admits', () => {
    renderSidebar();

    expect(capabilityHrefs()).toEqual(['/agents', '/skills', '/harness', '/vaults']);
  });

  it('leaves out a capability the authorization withholds', () => {
    auth.capabilities.can_use_skills = false;
    auth.capabilities.can_use_vault_secrets = false;
    renderSidebar();

    expect(capabilityHrefs()).toEqual(['/agents', null, '/harness', null]);
  });

  it('drops the whole section when no capability is admitted', () => {
    auth.capabilities.can_chat = false;
    auth.capabilities.can_use_agents = false;
    auth.capabilities.can_use_skills = false;
    auth.capabilities.can_use_vault_secrets = false;
    renderSidebar();

    expect(screen.queryByRole('button', { name: en.nav.capabilities })).toBeNull();
    expect(capabilityHrefs()).toEqual([null, null, null, null]);
  });

  it('collapses on demand and remembers the choice across a remount', () => {
    renderSidebar();
    expect(capabilityToggle().getAttribute('aria-expanded')).toBe('true');

    fireEvent.click(capabilityToggle());

    expect(capabilityToggle().getAttribute('aria-expanded')).toBe('false');
    expect(capabilityHrefs()).toEqual([null, null, null, null]);
    expect(window.localStorage.getItem('vibe-remote:caps-collapsed')).toBe('1');

    cleanup();
    renderSidebar();

    // The rows stay away after the remount, and expanding again writes the
    // choice back rather than merely dropping the key.
    expect(capabilityToggle().getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(capabilityToggle());
    expect(capabilityHrefs()).toEqual(['/agents', '/skills', '/harness', '/vaults']);
    expect(window.localStorage.getItem('vibe-remote:caps-collapsed')).toBe('0');
  });

  it('stays expanded when local storage cannot be read', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    try {
      renderSidebar();
      expect(capabilityToggle().getAttribute('aria-expanded')).toBe('true');
    } finally {
      getItem.mockRestore();
    }
  });
});

describe('Workbench sidebar search entry', () => {
  it('sits in the Projects header immediately before the add-project button', () => {
    renderSidebar();

    const search = screen.getByRole('button', { name: en.workbench.search.entry });
    const addProject = screen.getByRole('button', { name: en.workbench.addProject });
    expect(search.parentElement).toBe(addProject.parentElement);
    expect(search.nextElementSibling).toBe(addProject);
  });

  it('opens the palette through the callback the shell owns', () => {
    const onOpenSearch = vi.fn();
    renderSidebar({ onOpenSearch });

    fireEvent.click(screen.getByRole('button', { name: en.workbench.search.entry }));

    expect(onOpenSearch).toHaveBeenCalledTimes(1);
  });

  it('stays available to a member who cannot create projects', () => {
    auth.capabilities.can_manage_projects = false;
    auth.capabilities.can_use_files = false;
    renderSidebar();

    expect(screen.getByRole('button', { name: en.workbench.search.entry })).toBeTruthy();
    expect(screen.queryByRole('button', { name: en.workbench.addProject })).toBeNull();
  });
});
