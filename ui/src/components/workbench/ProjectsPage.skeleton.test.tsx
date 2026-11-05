/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchProject, WorkbenchSession } from '../../context/ApiContext';
import type { ProjectSessionsState } from '../../context/WorkbenchProjectsContext';
import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { clearMobileProjectsListSnapshot } from '../../lib/mobileProjectsListMemory';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { HANDOVER_CLASS } from '../ui/skeleton-handover';
import { ProjectsPage } from './ProjectsPage';

// The phone's Projects page loads behind skeletons, never a spinner
// (ui/design: mobile/projects/loading*): cards before the list is known, rows
// under an open project before its Sessions are, and two rows while Load more
// reads. Known rows show at once.

const tree = vi.hoisted(() => ({
  projects: null as WorkbenchProject[] | null,
  state: {} as Partial<ProjectSessionsState>,
}));

vi.mock('../../context/WorkbenchInboxContext', () => ({ useWorkbenchInbox: () => ({ unreadBySession: {} }) }));
vi.mock('../../context/WorkbenchProjectsContext', () => ({
  useWorkbenchProjectsActions: () => ({ renameProject: vi.fn(), archiveProject: vi.fn(), createSessionForProject: vi.fn(), renameSession: vi.fn() }),
  useWorkbenchProjectsTree: () => ({
    projects: tree.projects,
    projectsLoaded: tree.projects !== null,
    projectsError: null,
    refreshProjects: vi.fn(),
    sessionsOf: (): ProjectSessionsState => ({ sessions: null, loading: false, loadingMore: false, cursor: null, error: false, ...tree.state }),
    isExpanded: () => true,
    toggleExpanded: vi.fn(),
    loadMore: vi.fn(),
    reloadSessions: vi.fn(),
    creatingSession: () => false,
    reorderProjects: vi.fn(),
    isReorderingProjects: false,
  }),
}));
vi.mock('./useSessionActions', () => ({ useSessionActions: () => ({ actions: [], archiveDialog: null }) }));
vi.mock('./NewProjectDialog', () => ({ NewProjectDialog: () => null }));
vi.mock('./bots/BotStrip', () => ({ BotStrip: () => null }));
vi.mock('./ProjectAgentsMdDialog', () => ({ ProjectAgentsMdDialog: () => null }));
vi.mock('./ProjectSettingsDialog', () => ({ ProjectSettingsDialog: () => null }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const project: WorkbenchProject = {
  id: 'proj_a',
  scope_id: 'avibe::project::proj_a',
  display_name: 'Alpha',
  folder_path: '/tmp/alpha',
  created_at: '2026-08-01T00:00:00Z',
  last_active_at: null,
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
};
const session = (id: string) => ({
  id, scope_id: project.scope_id, project_id: project.id, title: id, agent_status: 'idle', pinned: false,
  status: 'active', last_active_at: '2026-08-19T00:00:00Z', updated_at: '2026-08-19T00:00:00Z',
}) as WorkbenchSession;

const app = () => (
  <InstanceAuthorizationContext.Provider
    value={{ remote: false, instanceKind: null, instanceRole: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES }}
  >
    <MemoryRouter initialEntries={['/projects']}>
      <ProjectsPage />
    </MemoryRouter>
  </InstanceAuthorizationContext.Provider>
);
const skeletonRegions = () => screen.queryAllByRole('status').filter((node) => node.querySelector(':scope > .skeleton-shimmer'));
const spinners = () => document.querySelectorAll('.animate-spin');

beforeEach(() => {
  clearMobileProjectsListSnapshot();
  tree.projects = [project];
  tree.state = {};
});
afterEach(cleanup);

describe('the Projects page', () => {
  it('draws project cards as a skeleton before the list is known, with no spinner', () => {
    tree.projects = null;
    render(app());
    expect(skeletonRegions()).toHaveLength(4);
    expect(spinners()).toHaveLength(0);
    // Each card is a project row's box: its padding and a one-line name at 14px.
    const card = skeletonRegions()[0];
    expect(card.className).toContain('rounded-xl');
    expect(card.querySelector('.px-4.py-3\\.5 .h-\\[1lh\\].text-sm')).toBeTruthy();
  });

  it('draws an open project\'s Sessions as rows of a Session row\'s size until they are known', () => {
    tree.state = { loading: true };
    render(app());
    const rows = skeletonRegions()[0].querySelectorAll(':scope > div:not(.skeleton-shimmer)');
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(row.className).toEqual(expect.stringContaining('px-3'));
      expect(row.className).toEqual(expect.stringContaining('py-2.5'));
      expect(row.className).toEqual(expect.stringContaining('gap-2.5'));
    }
    expect(spinners()).toHaveLength(0);
  });

  it('shows known Sessions at once, and fades in those that take a skeleton\'s place', () => {
    tree.state = { sessions: [session('cached')], loading: true };
    render(app());
    expect(screen.getByText('cached')).toBeTruthy();
    expect(skeletonRegions()).toHaveLength(0);
    expect(screen.getByText('cached').closest('.flex.flex-col.gap-0\\.5')!.className).not.toContain('starting:opacity-0');
    cleanup();

    tree.state = { loading: true };
    const next = render(app());
    tree.state = { sessions: [session('live')], loading: false };
    next.rerender(app());
    expect(screen.getByText('live').closest('.flex.flex-col.gap-0\\.5')!.className).toContain(HANDOVER_CLASS);
  });

  it('reads Load more behind two skeleton rows in place of the button, with no spinner', () => {
    tree.state = { sessions: [session('s1')], cursor: 'next', loadingMore: true };
    render(app());
    expect(screen.queryByText('projects.loadMore')).toBeNull();
    expect(skeletonRegions()[0].querySelectorAll(':scope > div:not(.skeleton-shimmer)')).toHaveLength(2);
    expect(spinners()).toHaveLength(0);
  });

  it('keeps a project row\'s height when its Session count arrives', () => {
    tree.state = { sessions: [session('s1')] };
    render(app());
    // No taller than the name's 20px line: 14px line, 2px vertical padding each side, 1px border each side.
    const badge = screen.getByText('Alpha').parentElement!.querySelector('.font-mono')!;
    expect(badge.className).toContain('leading-[14px]');
  });
});
