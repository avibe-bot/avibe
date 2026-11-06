/* @vitest-environment jsdom */
// The project list comes back where the reader left it. Coming back from a
// conversation in the same document puts the place back at once; coming back in
// a new document (a tab the browser discarded reloads, and going back loads the
// list afresh) waits for the list, then writes the place once, before the first
// frame that has it, and never again. Scrolling first wins over it.
//
// A new document is a fresh module registry: only sessionStorage carries over,
// as it does in the browser. React, the router and the page are imported after
// the reset so they are one instance.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchProject, WorkbenchSession } from '../../context/ApiContext';

// The list as the provider has it: `loaded` once a live list landed, `loading`
// while an open project waits for its rows (its skeleton is shorter), and the
// room the scroller then has below the top.
const tree = vi.hoisted(() => ({ projects: null as unknown[] | null, loaded: true, loading: false, room: 10_000 }));

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
  useWorkbenchProjectsTree: () => ({
    projects: tree.projects,
    projectsLoaded: tree.projects !== null && tree.loaded,
    projectsError: null,
    refreshProjects: vi.fn(),
    sessionsOf: () => ({ sessions: tree.loading ? null : rows, loading: tree.loading, loadingMore: false, cursor: null, error: false }),
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
  last_active_at: '2026-08-19T00:00:00Z',
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
};
const rows = Array.from({ length: 8 }, (_, index) => ({
  id: `ses_${index}`, scope_id: 'avibe::project::proj_a', project_id: 'proj_a', title: `Session ${index}`,
  status: 'active', pinned: false, agent_status: 'idle', workdir: '/tmp/alpha', native_session_id: `native_${index}`,
  created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z', last_active_at: '2026-08-19T00:00:00Z', metadata: {},
})) as unknown as WorkbenchSession[];

// The app shell's scroller, recording every write to its position.
let scroller: HTMLDivElement;
let writes: number[];
beforeEach(() => {
  writes = [];
  let top = 0;
  scroller = document.createElement('div');
  scroller.id = 'app-shell-scroll';
  Object.defineProperty(scroller, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (value: number) => {
      writes.push(value);
      top = value;
    },
  });
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => 600 });
  Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => 600 + tree.room });
  document.body.appendChild(scroller);
  Object.assign(tree, { projects: null, loaded: true, loading: false, room: 10_000 });
});

afterEach(async () => {
  const { cleanup } = await import('@testing-library/react');
  cleanup();
  scroller.remove();
  window.sessionStorage.clear();
  vi.resetModules();
});

/** A document of this tab: the page and everything it shares state with, fresh. */
async function newDocument() {
  vi.resetModules();
  const [{ render, act }, router, { ProjectsPage }, memory, { InstanceAuthorizationContext }, { OWNER_INSTANCE_CAPABILITIES }] = await Promise.all([
    import('@testing-library/react'),
    import('react-router-dom'),
    import('./ProjectsPage'),
    import('../../lib/mobileProjectsListMemory'),
    import('../../context/InstanceAuthorizationContext'),
    import('../../lib/sessionInfo'),
  ]);
  const page = () => (
    <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES }}>
      <router.MemoryRouter initialEntries={['/projects']}>
        <router.Routes><router.Route path="/projects" element={<ProjectsPage />} /></router.Routes>
      </router.MemoryRouter>
    </InstanceAuthorizationContext.Provider>
  );
  const mount = () => {
    const view = render(page());
    const rerender = () => act(() => { view.rerender(page()); });
    return { listArrives: () => { tree.projects = [project]; rerender(); }, rerender };
  };
  return { mount, memory };
}

describe('the project list\'s place', () => {
  it('in a new document, is written once, when the list is there to hold it', async () => {
    // The previous document left the list for a conversation, 420 down.
    const before = await newDocument();
    before.memory.holdMobileProjectsListForChatReturn({ visibleCounts: { proj_a: 16 }, scrollTop: 420 });

    const { mount } = await newDocument();
    const { listArrives, rerender } = mount();
    // Nothing is written over the skeleton, which has no room for the place.
    expect(writes).toEqual([]);
    listArrives();
    expect(writes).toEqual([420]);
    // Later renders leave the reader's scrolling alone, even a list that goes and
    // comes back (an authorization change drops it).
    rerender();
    tree.projects = null;
    rerender();
    listArrives();
    expect(writes).toEqual([420]);
  });

  it('in a new document, waits for an open project\'s rows when its skeleton is too short to hold the place', async () => {
    const before = await newDocument();
    before.memory.rememberMobileProjectsListScroll(420);

    const { mount } = await newDocument();
    const { listArrives, rerender } = mount();
    // Painted from the device; the open project's rows were not stored.
    Object.assign(tree, { loaded: false, loading: true, room: 200 });
    listArrives();
    expect(writes).toEqual([]);
    // Its rows come with the live list, and the list can hold the place.
    Object.assign(tree, { loaded: true, loading: false, room: 900 });
    rerender();
    expect(writes).toEqual([420]);
    rerender();
    expect(writes).toEqual([420]);
  });

  it('in a new document, settles for the nearest place a settled list has', async () => {
    const before = await newDocument();
    before.memory.rememberMobileProjectsListScroll(420);

    const { mount } = await newDocument();
    const { listArrives } = mount();
    tree.room = 200;
    listArrives();
    expect(writes).toEqual([420]);
  });

  it('in a new document, is given up when the reader scrolls before the list is there', async () => {
    const before = await newDocument();
    before.memory.rememberMobileProjectsListScroll(420);

    const { mount } = await newDocument();
    const { listArrives } = mount();
    window.dispatchEvent(new Event('touchmove'));
    listArrives();
    expect(writes).toEqual([]);
  });

  it('is not carried past a departure to another page', async () => {
    const before = await newDocument();
    before.memory.rememberMobileProjectsListScroll(420);
    before.memory.forgetMobileProjectsListUnlessPreserved('/inbox');

    const { mount } = await newDocument();
    mount().listArrives();
    expect(writes).not.toContain(420);
  });

  it('in the same document, comes back at once from a conversation, as before', async () => {
    tree.projects = [project];
    const { mount, memory } = await newDocument();
    memory.holdMobileProjectsListForChatReturn({ visibleCounts: {}, scrollTop: 180 });
    mount();
    expect(writes[0]).toBe(180);
    expect(writes.every((top) => top === 180)).toBe(true);
  });
});
