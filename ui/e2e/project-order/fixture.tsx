import { createRoot } from 'react-dom/client';
import { createHashRouter, RouterProvider } from 'react-router-dom';
import { ApiProvider, type WorkbenchProject } from '../../src/context/ApiContext';
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
const names = ['Avibe', 'chainbot', 'Research', 'Documents', 'Design', 'Infrastructure', 'Sandbox', 'Archive'];
const projects: WorkbenchProject[] = names.map((display_name, index) => ({
  id: `project-${index}`, scope_id: `scope-${index}`, display_name, folder_path: `/fixture/${index}`,
  created_at: `2026-01-0${index + 1}T00:00:00Z`, last_active_at: null, archived: false,
  capabilities: { can_chat: true, has_folder: true },
}));
const storageKey = 'project-order-fixture';
const readProjects = () => {
  const saved = JSON.parse(localStorage.getItem(storageKey) ?? 'null') as string[] | null;
  return saved ? saved.map((id) => projects.find((p) => p.id === id)!) : projects;
};
const streams = new Set<EventTarget>();
class FixtureEvents extends EventTarget {
  readyState = 1;
  constructor() { super(); streams.add(this); }
  close() { streams.delete(this); this.readyState = 2; }
}
Object.defineProperty(window, 'EventSource', { value: FixtureEvents });
const invalidate = () => streams.forEach((stream) => stream.dispatchEvent(new MessageEvent('projects.changed', {
  data: JSON.stringify({ type: 'projects.changed', data: {} }),
})));
window.addEventListener('storage', (event) => { if (event.key === storageKey) invalidate(); });

// Hermetic transport: the actual API client/provider and both real project
// surfaces run here, but no request can reach a workstation Avibe service.
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin);
  const reply = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  if (url.pathname === '/api/csrf-token') return reply({ csrf_token: 'fixture' });
  if (url.pathname === '/api/projects/order') {
    const payload = JSON.parse(String(init?.body));
    if (JSON.stringify(payload.expected_order) !== JSON.stringify(readProjects().map((p) => p.id))) {
      return reply({ code: 'project_order_conflict', error: 'stale' }, 409);
    }
    localStorage.setItem(storageKey, JSON.stringify(payload.order));
    invalidate();
    return reply({ projects: readProjects() });
  }
  if (url.pathname === '/api/projects' || url.pathname === '/api/workbench/projects-bootstrap') return reply({ projects: readProjects(), sessions: {} });
  if (url.pathname === '/api/sessions') {
    const projectId = url.searchParams.get('project_id');
    return reply({ sessions: Array.from({ length: 8 }, (_, index) => ({
      id: `${projectId}-session-${index}`, title: `Conversation ${index + 1}`, project_id: projectId,
      scope_id: projects.find((p) => p.id === projectId)?.scope_id, status: 'active', agent_status: 'idle',
      pinned: false, visibility: 'foreground', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
      last_active_at: '2026-01-01T00:00:00Z', metadata: {},
    })), next_before_id: null });
  }
  return reply({});
};

export function Fixture() {
  return <InstanceAuthorizationContext.Provider value={{
    remote: false, instanceKind: null, instanceRole: 'owner',
    capabilities: { ...OWNER_INSTANCE_CAPABILITIES, can_read_instance: false },
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
createRoot(document.getElementById('root')!).render(<RouterProvider router={createHashRouter([{ path: '*', element: <Fixture /> }])} />);
