import { createRoot } from 'react-dom/client';
import { createHashRouter, RouterProvider } from 'react-router-dom';
import { ApiProvider, type WorkbenchProject, type WorkbenchSession } from '../../src/context/ApiContext';
import { WorkbenchProjectsProvider } from '../../src/context/WorkbenchProjectsProvider';
import { WorkbenchInboxContext } from '../../src/context/WorkbenchInboxContext';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { ToastContext } from '../../src/context/ToastContext';
import { WindowManagerContext, type WindowManagerValue } from '../../src/context/WindowManagerContext';
import { UnsavedChangesProvider } from '../../src/context/UnsavedChangesProvider';
import { WorkbenchSidebar } from '../../src/components/workbench/WorkbenchSidebar';
import { ProjectsPage } from '../../src/components/workbench/ProjectsPage';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import '../../src/i18n';
import '../../src/index.css';

const noop = () => {};

// Two projects at the same tree depth, so the rows the spec measures against
// each other are siblings in layout terms. The first is writable and the
// second is not: `canManageMetadata` is `can_manage_projects || project.can_chat`,
// so denying the instance the former and the project the latter produces the
// read-only row without touching any other permission. Titles are non-ASCII
// because that is what the reported screenshot shows, and Latin-only fixtures
// flatter both truncation and measurement.
const projects: WorkbenchProject[] = [
  {
    id: 'project-writable', scope_id: 'scope-writable', display_name: '可写项目',
    folder_path: '/fixture/可写项目', created_at: '2026-01-01T00:00:00Z', last_active_at: null,
    archived: false, capabilities: { can_chat: true, has_folder: true },
  },
  {
    id: 'project-readonly', scope_id: 'scope-readonly', display_name: '只读项目',
    folder_path: '/fixture/只读项目', created_at: '2026-01-02T00:00:00Z', last_active_at: null,
    archived: false, capabilities: { can_chat: false, has_folder: true },
  },
];

type Seed = { id: string; title: string; agent_status: WorkbenchSession['agent_status'] };

// Every status the dot maps, plus a second running session so the spec can hold
// one running row selected and another running row unselected at the same time.
const SEEDS: Record<string, Seed[]> = {
  'project-writable': [
    { id: 'writable-running', title: '实例描述字段：实现', agent_status: 'running' },
    { id: 'writable-idle', title: '修复组织成员 Model', agent_status: 'idle' },
    { id: 'writable-running-2', title: '第二个运行中的会话', agent_status: 'running' },
    { id: 'writable-failed', title: '失败的会话', agent_status: 'failed' },
  ],
  'project-readonly': [
    { id: 'readonly-running', title: '只读运行中的会话', agent_status: 'running' },
    { id: 'readonly-idle', title: '只读空闲会话', agent_status: 'idle' },
  ],
};

const session = (projectId: string, seed: Seed): WorkbenchSession => ({
  id: seed.id, title: seed.title, project_id: projectId,
  scope_id: projects.find((p) => p.id === projectId)?.scope_id,
  status: 'active', agent_status: seed.agent_status, pinned: false, visibility: 'foreground',
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  last_active_at: '2026-01-01T00:00:00Z', metadata: {},
} as WorkbenchSession);

// One mutable store behind every read, because the product has one too. The
// provider re-reads a single row after a status event, so a fixture that only
// answered the list would hand that read an empty session and blank the row.
const rows = new Map<string, WorkbenchSession>(
  Object.entries(SEEDS).flatMap(([projectId, seeds]) =>
    seeds.map((seed) => [seed.id, session(projectId, seed)] as const),
  ),
);
const rowsOf = (projectId: string) => [...rows.values()].filter((row) => row.project_id === projectId);

const streams = new Set<EventTarget>();
class FixtureEvents extends EventTarget {
  readyState = 1;
  constructor() { super(); streams.add(this); }
  close() { streams.delete(this); this.readyState = 2; }
}
Object.defineProperty(window, 'EventSource', { value: FixtureEvents });

// The product's own live path: the spec flips a status through the SSE event the
// service emits, not through a prop, so what it measures is the same update the
// user gets while a session starts or stops working.
declare global {
  interface Window {
    setSessionStatus: (sessionId: string, agentStatus: WorkbenchSession['agent_status']) => void;
  }
}
window.setSessionStatus = (session_id, agent_status) => {
  const row = rows.get(session_id);
  if (row) rows.set(session_id, { ...row, agent_status });
  streams.forEach((stream) => stream.dispatchEvent(new MessageEvent('session.status', {
    data: JSON.stringify({ type: 'session.status', data: { session_id, agent_status } }),
  })));
};

// Hermetic transport: the real API client, provider and both session surfaces
// run here, but no request can reach a workstation Avibe service or user state.
window.fetch = async (input) => {
  const url = new URL(
    typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
    location.origin,
  );
  const reply = (data: unknown) => new Response(JSON.stringify(data), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
  if (url.pathname === '/api/csrf-token') return reply({ csrf_token: 'fixture' });
  if (url.pathname === '/api/projects' || url.pathname === '/api/workbench/projects-bootstrap') {
    return reply({ projects, sessions: {} });
  }
  if (url.pathname === '/api/sessions') {
    return reply({ sessions: rowsOf(url.searchParams.get('project_id') ?? ''), next_before_id: null });
  }
  const row = rows.get(decodeURIComponent(url.pathname.replace('/api/sessions/', '')));
  if (row) return reply(row);
  return reply({});
};

export function Fixture() {
  return <InstanceAuthorizationContext.Provider value={{
    remote: false, instanceKind: null, instanceRole: 'owner',
    capabilities: { ...OWNER_INSTANCE_CAPABILITIES, can_manage_projects: false },
  }}><ToastContext.Provider value={{ showToast: noop }}><ApiProvider><UnsavedChangesProvider>
    <WorkbenchProjectsProvider><WorkbenchInboxContext.Provider value={{
      inboxSessions: [], unreadBySession: {}, totalUnread: 0, unreadSessions: 0, nextCursor: null,
      loading: false, loadingMore: false, refresh: async () => {}, loadMore: async () => {},
      markRead: async () => {}, activateFeed: () => noop,
    }}><WindowManagerContext.Provider value={{ focusedId: null, focusCanvas: noop, openApp: noop } as unknown as WindowManagerValue}>
      <div className="flex h-dvh overflow-hidden bg-background text-foreground">
        <aside className="hidden h-full w-64 shrink-0 border-r border-border md:flex" data-testid="desktop-tree"><WorkbenchSidebar /></aside>
        <main id="app-shell-scroll" className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4 md:hidden" data-testid="mobile-tree"><ProjectsPage /></main>
      </div>
    </WindowManagerContext.Provider></WorkbenchInboxContext.Provider></WorkbenchProjectsProvider>
  </UnsavedChangesProvider></ApiProvider></ToastContext.Provider></InstanceAuthorizationContext.Provider>;
}

createRoot(document.getElementById('root')!).render(
  <RouterProvider router={createHashRouter([{ path: '*', element: <Fixture /> }])} />,
);
