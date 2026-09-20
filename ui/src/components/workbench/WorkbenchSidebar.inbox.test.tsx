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

vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: (options?: { feed?: boolean }) => {
    hookCalls.inbox(options);
    return inbox;
  },
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({
    capabilities: {
      can_chat: true,
      can_manage_projects: true,
      can_use_agents: true,
      can_use_skills: true,
      can_use_vault_secrets: true,
    },
  }),
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

const renderSidebar = (active = true) => render(
  <I18nextProvider i18n={i18n}>
    <MemoryRouter initialEntries={['/']}>
      <RouteSurfaceActivityBoundary active={active}>
        <WorkbenchSidebar />
      </RouteSurfaceActivityBoundary>
    </MemoryRouter>
  </I18nextProvider>,
);

const inboxIcon = () => screen.getByRole('link', { name: en.workbench.nav.inbox }).querySelector('svg')!;

beforeEach(() => {
  inbox.totalUnread = 0;
  inbox.unreadSessions = 0;
  inbox.inboxSessions = [];
  inbox.unreadBySession = {};
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
    renderSidebar();

    const toggle = screen.getByRole('button', { name: en.nav.capabilities });
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('Agent OS')).toBeTruthy();
    expect(screen.getByRole('link', { name: en.workbench.nav.agents })).toBeTruthy();

    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('link', { name: en.workbench.nav.agents })).toBeNull();

    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('link', { name: en.workbench.nav.agents })).toBeTruthy();
  });
});
