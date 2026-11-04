// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  ProjectDefaultAgent,
  WorkbenchEventHandlers,
  WorkbenchProject,
  WorkbenchProjectsBootstrap,
} from './ApiContext';
import { InstanceAuthorizationProvider } from './InstanceAuthorizationProvider';
import { useWorkbenchProjectsTree, type WorkbenchProjectsTree } from './WorkbenchProjectsContext';
import { WorkbenchProjectsProvider } from './WorkbenchProjectsProvider';
import {
  clearLocalSnapshots,
  readLocalSnapshot,
  setLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from '../lib/localSnapshots';
import { resetCoalescedWrites } from '../lib/useCoalescedWrite';

// Opening the sidebar paints the project list from the last snapshot (docs/plans/
// web-ui-local-cache.md) and the live bootstrap then owns it. The risks are the
// hand-over in both directions: a cached project surviving the live answer that
// left it out, and something the server never said (a pick in flight, another
// reader's list) being shown or stored as if it had.

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const KEY = 'projects:bootstrap';

const project = (id: string, over: Partial<WorkbenchProject> = {}): WorkbenchProject => ({
  id,
  scope_id: `scope_${id}`,
  display_name: id,
  folder_path: `/tmp/${id}`,
  created_at: '2026-11-01T00:00:00Z',
  last_active_at: null,
  archived: false,
  default_agent: null,
  capabilities: { can_chat: true, has_folder: true },
  ...over,
});

const answer = (...projects: WorkbenchProject[]): WorkbenchProjectsBootstrap => ({ projects, sessions: {} });

async function storeSnapshot(reader: string, value: unknown, schema = 1) {
  setLocalSnapshotPrincipal(reader);
  await writeLocalSnapshot(KEY, schema, value);
  setLocalSnapshotPrincipal(null);
}

// Read as the reader the provider currently stores for.
const stored = () => readLocalSnapshot(KEY, 1, (value) => value as { projects: WorkbenchProject[] });

// The bootstrap answers only when the test says so.
function fakeApi() {
  const pending: ((result: WorkbenchProjectsBootstrap) => void)[] = [];
  const handlers: WorkbenchEventHandlers[] = [];
  const api = {
    getWorkbenchProjectsBootstrap: vi.fn(
      () => new Promise<WorkbenchProjectsBootstrap>((resolve) => { pending.push(resolve); }),
    ),
    updateProject: vi.fn(() => new Promise(() => {})),
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  apiRef.current = api;
  return {
    api,
    handlers,
    respond: async (result: WorkbenchProjectsBootstrap) => {
      await act(async () => { pending.shift()?.(result); });
      await settle();
    },
  };
}

function mount(reader: string | null, options: { active?: boolean } = {}) {
  const state = { current: null as WorkbenchProjectsTree | null };
  const Probe = () => {
    const tree = useWorkbenchProjectsTree({ active: options.active ?? true });
    useEffect(() => { state.current = tree; });
    return null;
  };
  const tree = (authorId: string | null) => (
    <WorkbenchProjectsProvider>
      <InstanceAuthorizationProvider session={{ remote: false, instance_kind: null, author_id: authorId }}>
        <Probe />
      </InstanceAuthorizationProvider>
    </WorkbenchProjectsProvider>
  );
  const view = render(tree(reader));
  return { state, switchReader: (authorId: string | null) => view.rerender(tree(authorId)) };
}

const ids = (tree: WorkbenchProjectsTree | null) => tree?.projects?.map((p) => p.id) ?? null;

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
});

afterEach(async () => {
  cleanup();
  resetCoalescedWrites();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('the projects local snapshot', () => {
  it('paints the last list before the live bootstrap, then lets the live answer replace it and stores that', async () => {
    await storeSnapshot('local', { projects: [project('kept'), project('gone')] });
    const { api, respond } = fakeApi();
    const { state } = mount('local');
    await settle();

    // The server has not answered: the snapshot is on screen, and it is not loaded.
    expect(api.getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(1);
    expect(ids(state.current)).toEqual(['kept', 'gone']);
    expect(state.current!.projectsLoaded).toBe(false);

    // The server no longer returns `gone` (archived, or no longer shared).
    await respond(answer(project('new'), project('kept', { display_name: 'Kept, renamed' })));
    expect(ids(state.current)).toEqual(['new', 'kept']);
    expect(state.current!.projects![1].display_name).toBe('Kept, renamed');
    expect(state.current!.projectsLoaded).toBe(true);

    // The live answer becomes the next cold start's snapshot.
    expect((await stored())?.projects.map((p) => p.id)).toEqual(['new', 'kept']);
  });

  it('stores the route the server answered, not a pick still in flight', async () => {
    const { api, respond } = fakeApi();
    const { state } = mount('local');
    await settle();
    await respond(answer(project('a')));

    const pick: ProjectDefaultAgent = {
      agent_backend: null, agent_id: 'agt_pick', agent_name: 'pick', agent_variant: null, model: null, reasoning_effort: null,
    };
    act(() => state.current!.setProjectDefaultAgent('a', pick));
    await settle();
    expect(api.updateProject).toHaveBeenCalledTimes(1);

    // A bootstrap read after the pick still answers the route the server holds.
    let refreshed!: Promise<void>;
    act(() => { refreshed = state.current!.refreshProjects(); });
    await respond(answer(project('a', { display_name: 'A, re-read' })));
    await act(async () => { await refreshed; });

    // On screen the pick outranks the read; in the snapshot the read is all there is.
    expect(state.current!.projects![0].default_agent?.agent_id).toBe('agt_pick');
    const record = (await stored())?.projects[0];
    expect(record?.display_name).toBe('A, re-read');
    expect(record?.default_agent).toBeNull();
  });

  it('drops a painted list the moment a different reader is confirmed, before any live answer', async () => {
    await storeSnapshot('remote:alice', { projects: [project('alice_project')] });
    // The live read never answers: whatever is on screen must not be Alice's.
    fakeApi();
    // A cold start replays Alice's session...
    const { state, switchReader } = mount('remote:alice');
    await settle();
    expect(ids(state.current)).toEqual(['alice_project']);

    // ...and the guard's check confirms Bob.
    switchReader('remote:bob');
    await settle();
    expect(state.current!.projects).toBeNull();
    expect(state.current!.projectsLoaded).toBe(false);
  });

  it('stores the next live bootstrap for a reader confirmed after the list went live, and nothing before it', async () => {
    const { respond } = fakeApi();
    // The live bootstrap lands before the guard's check has answered.
    const { state, switchReader } = mount('remote:alice');
    await settle();
    await respond(answer(project('first')));
    expect(ids(state.current)).toEqual(['first']);
    expect((await stored())?.projects.map((p) => p.id)).toEqual(['first']);

    // The check confirms Bob. The list on screen was answered before he was
    // named, so it is not stored as his...
    switchReader('remote:bob');
    await settle();
    expect(await stored()).toBeNull();

    // ...and his own next live bootstrap is.
    let refreshed!: Promise<void>;
    act(() => { refreshed = state.current!.refreshProjects(); });
    await respond(answer(project('bob_project')));
    await act(async () => { await refreshed; });
    expect((await stored())?.projects.map((p) => p.id)).toEqual(['bob_project']);
  });

  it.each([
    ['from an older schema', { projects: [project('old')] }, 0],
    ['whose rows are unreadable', { projects: [{ id: 'broken' }] }, 1],
    ['without a list', { projects: 'none' }, 1],
    ['with an empty list', { projects: [] }, 1],
  ])('paints nothing from a record %s, and stores the live answer over it', async (_case, value, schema) => {
    await storeSnapshot('local', value, schema);
    setLocalSnapshotPrincipal('local');
    expect(await readLocalSnapshot(KEY, schema, (raw) => raw)).not.toBeNull();
    setLocalSnapshotPrincipal(null);

    const { respond } = fakeApi();
    const { state } = mount('local');
    await settle();
    // The loading state, and for an empty list not the empty state either: that
    // is the live answer's to show.
    expect(state.current!.projects).toBeNull();

    await respond(answer(project('live')));
    expect(ids(state.current)).toEqual(['live']);
    expect((await stored())?.projects.map((p) => p.id)).toEqual(['live']);
  });

  it('paints nothing for a document that does not read the tree', async () => {
    await storeSnapshot('local', { projects: [project('a')] });
    const { api } = fakeApi();
    // The reader is named after the provider is up, as the route guard does.
    const { state, switchReader } = mount(null, { active: false });
    await settle();
    switchReader('local');
    await settle();
    expect(api.getWorkbenchProjectsBootstrap).not.toHaveBeenCalled();
    expect(state.current!.projects).toBeNull();
  });

  it('never paints a snapshot read before an authorization change dropped the tree', async () => {
    await storeSnapshot('local', { projects: [project('revoked')] });
    const { handlers } = fakeApi();
    const { state } = mount('local');
    // The snapshot read is in flight when the change arrives.
    act(() => { for (const handler of handlers) handler.onAuthorizationChanged?.({}); });
    await settle();
    expect(state.current!.projects).toBeNull();
  });
});
