/* @vitest-environment jsdom */

import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import type { ProjectSessionsState } from '../../context/WorkbenchProjectsContext';
import type { WorkbenchProject, WorkbenchSession } from '../../context/ApiContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import {
  clearMobileProjectsListSnapshot,
  holdMobileProjectsListForChatReturn,
  readMobileProjectsListSnapshot,
} from '../../lib/mobileProjectsListMemory';
import { ProjectsPage } from './ProjectsPage';

const tree = vi.hoisted(() => ({
  projects: [] as WorkbenchProject[],
  sessions: [] as WorkbenchSession[],
  loadMore: vi.fn(),
  toggleExpanded: vi.fn(),
}));

vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({ unreadBySession: {} }),
}));

vi.mock('../../context/WorkbenchProjectsContext', () => ({
  useWorkbenchProjectsActions: () => ({
    renameProject: vi.fn(),
    archiveProject: vi.fn(),
    createSessionForProject: vi.fn(),
    renameSession: vi.fn(),
  }),
  useWorkbenchProjectsTree: () => {
    const [expanded, setExpanded] = useState<Set<string>>(new Set([tree.projects[0]?.id]));
    const state: ProjectSessionsState = {
      sessions: tree.sessions,
      loading: false,
      loadingMore: false,
      cursor: 'next',
      error: false,
    };
    return {
      projects: tree.projects,
      projectsError: null,
      refreshProjects: vi.fn(),
      sessionsOf: () => state,
      isExpanded: (id: string) => expanded.has(id),
      toggleExpanded: (id: string) => {
        tree.toggleExpanded(id);
        setExpanded((prev) => {
          const next = new Set(prev);
          if (next.has(id)) next.delete(id);
          else next.add(id);
          return next;
        });
      },
      loadMore: tree.loadMore,
      reloadSessions: vi.fn(),
      creatingSession: () => false,
    };
  },
}));

vi.mock('./useSessionActions', () => ({
  useSessionActions: () => ({ actions: [], archiveDialog: null }),
}));

vi.mock('./NewProjectDialog', () => ({ NewProjectDialog: () => null }));
vi.mock('./ProjectAgentsMdDialog', () => ({ ProjectAgentsMdDialog: () => null }));
vi.mock('./ProjectSettingsDialog', () => ({ ProjectSettingsDialog: () => null }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const project: WorkbenchProject = {
  id: 'proj_a',
  scope_id: 'avibe::project::proj_a',
  display_name: 'Alpha',
  folder_path: '/tmp/alpha',
  created_at: '2026-08-01T00:00:00Z',
  last_active_at: '2026-08-19T00:00:00Z',
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
};

const session = (index: number): WorkbenchSession =>
  ({
    id: `ses_${index}`,
    scope_id: 'avibe::project::proj_a',
    project_id: 'proj_a',
    title: `Session ${index}`,
    status: 'active',
    pinned: false,
    agent_status: 'idle',
    workdir: '/tmp/alpha',
    native_session_id: `native_${index}`,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    last_active_at: '2026-08-19T00:00:00Z',
    metadata: {},
  }) as WorkbenchSession;

function renderPage() {
  const view = render(
    <InstanceAuthorizationContext.Provider
      value={{
        remote: false,
        instanceKind: null,
        instanceRole: 'owner',
        capabilities: OWNER_INSTANCE_CAPABILITIES,
      }}
    >
      <MemoryRouter initialEntries={['/projects']}>
        <ProjectsPage />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
  return view;
}

beforeEach(() => {
  tree.projects = [project];
  tree.sessions = Array.from({ length: 16 }, (_, index) => session(index + 1));
  tree.loadMore.mockReset();
  tree.toggleExpanded.mockReset();
  clearMobileProjectsListSnapshot();
});

afterEach(() => {
  cleanup();
  clearMobileProjectsListSnapshot();
});

describe('ProjectsPage mobile session window', () => {
  it('reveals the next cached page without fetching until the cache is exhausted', async () => {
    const user = userEvent.setup();
    const view = renderPage();

    expect(screen.getByText('Session 8')).toBeTruthy();
    expect(screen.queryByText('Session 9')).toBeNull();

    await user.click(screen.getByRole('button', { name: 'projects.loadMore' }));
    expect(screen.getByText('Session 16')).toBeTruthy();
    expect(tree.loadMore).not.toHaveBeenCalled();
    view.unmount();
  });

  it('captures the revealed window before opening a session', async () => {
    const user = userEvent.setup();
    const view = renderPage();

    await user.click(screen.getByRole('button', { name: 'projects.loadMore' }));
    await user.click(screen.getByText('Session 12'));

    expect(readMobileProjectsListSnapshot().visibleCounts).toEqual({ proj_a: 16 });
    view.unmount();
  });

  it('resumes a held window after returning from chat', () => {
    holdMobileProjectsListForChatReturn({ visibleCounts: { proj_a: 16 }, scrollTop: 180 });
    const view = renderPage();
    expect(screen.getByText('Session 16')).toBeTruthy();
    view.unmount();
  });

  it('forgets a project window when that project is collapsed and expanded again', async () => {
    const user = userEvent.setup();
    const view = renderPage();

    await user.click(screen.getByRole('button', { name: 'projects.loadMore' }));
    expect(screen.getByText('Session 16')).toBeTruthy();

    await user.click(screen.getByText('Alpha'));
    expect(screen.queryByText('Session 1')).toBeNull();

    await user.click(screen.getByText('Alpha'));
    expect(screen.getByText('Session 8')).toBeTruthy();
    expect(screen.queryByText('Session 9')).toBeNull();
    view.unmount();
  });
});
