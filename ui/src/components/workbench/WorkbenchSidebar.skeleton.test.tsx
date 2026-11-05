/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchProject, WorkbenchSession } from '../../context/ApiContext';
import type { ProjectSessionsState } from '../../context/WorkbenchProjectsContext';

// The sidebar's project tree loads behind skeletons, never a spinner
// (ui/design: sidebar/workbench-sidebar/loading-*): the list before it is known,
// an expanded project before its Sessions are, and Load more while it reads. A
// window that is known (live or from the cache) shows its rows and no skeleton.

const tree = vi.hoisted(() => ({
  projects: null as WorkbenchProject[] | null,
  sessions: {} as Record<string, Partial<ProjectSessionsState>>,
  expanded: new Set<string>(),
}));

vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({ totalUnread: 0, badgeTotal: 0, unreadSessions: 0, inboxSessions: [], unreadBySession: {}, markRead: vi.fn() }),
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({
    capabilities: { can_chat: true, can_manage_projects: true, can_use_files: true, can_use_agents: true, can_use_skills: true, can_use_vault_secrets: true },
  }),
}));
vi.mock('../../context/WorkbenchProjectsContext', () => ({
  useWorkbenchProjectsTree: () => ({
    projects: tree.projects,
    projectsError: null,
    sessionsOf: (id: string): ProjectSessionsState => ({
      sessions: null, loading: false, loadingMore: false, cursor: null, error: false, ...tree.sessions[id],
    }),
    isExpanded: (id: string) => tree.expanded.has(id),
    toggleExpanded: vi.fn(),
    loadMore: vi.fn(),
    creatingSession: () => false,
    createSessionForProject: vi.fn(),
    renameProject: vi.fn(),
    archiveProject: vi.fn(),
    reorderProjects: vi.fn(),
    isReorderingProjects: false,
  }),
  useWorkbenchProjectsActions: () => ({ renameSession: vi.fn() }),
}));
vi.mock('../../context/WindowManagerContext', () => ({ useWindowManager: () => ({ open: vi.fn() }) }));
vi.mock('../../context/useUnsavedChangesActionGuard', () => ({
  useUnsavedChangesActionGuard: () => (run: () => void) => run,
}));
vi.mock('./useSessionActions', () => ({ useSessionActions: () => ({ actions: [], archiveDialog: null }) }));
vi.mock('./ProjectSettingsDialog', () => ({ ProjectSettingsDialog: () => null }));
vi.mock('./ProjectAgentsMdDialog', () => ({ ProjectAgentsMdDialog: () => null }));
vi.mock('./bots/BotStrip', () => ({ BotStrip: () => null }));

import en from '../../i18n/en.json';
import { HANDOVER_CLASS } from '../ui/skeleton-handover';
import { WorkbenchSidebar } from './WorkbenchSidebar';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const project = (id: string): WorkbenchProject => ({
  id,
  scope_id: `avibe::project::${id}`,
  display_name: id,
  folder_path: `/tmp/${id}`,
  created_at: '2026-11-01T00:00:00Z',
  last_active_at: null,
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
});
const session = (id: string) => ({
  id, scope_id: 'avibe::project::a', project_id: 'a', title: id, agent_status: 'idle', pinned: false,
  last_active_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z',
}) as WorkbenchSession;

const app = () => (
  <I18nextProvider i18n={i18n}>
    <MemoryRouter initialEntries={['/']}>
      <WorkbenchSidebar />
    </MemoryRouter>
  </I18nextProvider>
);

const loadingRegion = (label: string) => screen.queryByRole('status', { name: label });
const spinners = () => document.querySelectorAll('.animate-spin');
/** The nearest box around ``node`` that fades in (the handover). */
const fadingBox = (node: HTMLElement): HTMLElement | null => {
  for (let el: HTMLElement | null = node; el; el = el.parentElement) {
    if (el.className.includes('starting:opacity-0')) return el;
  }
  return null;
};

beforeEach(() => {
  window.localStorage.clear();
  tree.projects = [project('a')];
  tree.sessions = {};
  tree.expanded = new Set(['a']);
});
afterEach(cleanup);

describe('the sidebar project tree', () => {
  it('draws project rows as a skeleton before the list is known, with no spinner', () => {
    tree.projects = null;
    render(app());
    expect(loadingRegion(en.workbench.projectsLoading)).toBeTruthy();
    expect(spinners()).toHaveLength(0);
  });

  it('draws an expanded project\'s Sessions as rows of a Session row\'s size until they are known', () => {
    tree.sessions = { a: { loading: true } };
    render(app());
    const region = loadingRegion(en.workbench.sessionsLoading)!;
    expect(region).toBeTruthy();
    expect(spinners()).toHaveLength(0);
    // The box of `SessionRow`: padding, the indent and its accent rule, and a
    // title line one line tall at 12px.
    const rows = region.querySelectorAll(':scope > div:not(.skeleton-shimmer)');
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(row.className).toEqual(expect.stringContaining('py-1.5'));
      expect(row.className).toEqual(expect.stringContaining('pr-2.5'));
      expect(row.className).toEqual(expect.stringContaining('border-l-2'));
      expect(row.className).toEqual(expect.stringContaining('pl-[24px]'));
      expect(row.querySelector('.h-\\[1lh\\].text-\\[12px\\]')).toBeTruthy();
    }
  });

  it('shows known Sessions at once, without a skeleton, even while the live read is out', () => {
    // A window painted from the cache: rows known, live read in flight.
    tree.sessions = { a: { sessions: [session('s1'), session('s2')], loading: true } };
    render(app());
    expect(screen.getByText('s1')).toBeTruthy();
    expect(loadingRegion(en.workbench.sessionsLoading)).toBeNull();
    expect(fadingBox(screen.getByText('s1'))).toBeNull();
  });

  it('fades in Sessions that take the skeleton\'s place', () => {
    tree.sessions = { a: { loading: true } };
    const view = render(app());
    tree.sessions = { a: { sessions: [session('s1')], loading: false } };
    view.rerender(app());
    expect(loadingRegion(en.workbench.sessionsLoading)).toBeNull();
    expect(fadingBox(screen.getByText('s1'))?.className).toContain(HANDOVER_CLASS);
  });

  it('reads Load more behind two skeleton rows in place of the button, with no spinner', () => {
    tree.sessions = { a: { sessions: [session('s1')], cursor: 'next', loadingMore: true } };
    render(app());
    expect(screen.queryByText(en.workbench.sessionsLoadMore)).toBeNull();
    const region = loadingRegion(en.workbench.sessionsLoading)!;
    expect(region.querySelectorAll(':scope > div:not(.skeleton-shimmer)')).toHaveLength(2);
    expect(spinners()).toHaveLength(0);
  });
});
