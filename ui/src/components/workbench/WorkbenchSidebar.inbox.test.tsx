/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const inbox = vi.hoisted(() => ({
  totalUnread: 0,
  unreadSessions: 0,
  inboxSessions: [] as unknown[],
  unreadBySession: {} as Record<string, number>,
  markRead: vi.fn(),
}));

vi.mock('../../context/WorkbenchInboxContext', () => ({ useWorkbenchInbox: () => inbox }));
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
  useWorkbenchProjectsTree: () => ({
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
  }),
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

const renderSidebar = () => render(
  <I18nextProvider i18n={i18n}>
    <MemoryRouter initialEntries={['/']}>
      <WorkbenchSidebar />
    </MemoryRouter>
  </I18nextProvider>,
);

const inboxIcon = () => screen.getByRole('link', { name: en.workbench.nav.inbox }).querySelector('svg')!;

beforeEach(() => {
  inbox.totalUnread = 0;
  inbox.unreadSessions = 0;
  inbox.inboxSessions = [];
  inbox.unreadBySession = {};
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
});
