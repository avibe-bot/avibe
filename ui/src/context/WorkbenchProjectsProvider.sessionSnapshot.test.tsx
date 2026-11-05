// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchEventHandlers, WorkbenchProject, WorkbenchSession } from './ApiContext';
import { InstanceAuthorizationProvider } from './InstanceAuthorizationProvider';
import { useWorkbenchProjectsTree, type WorkbenchProjectsTree } from './WorkbenchProjectsContext';
import { WorkbenchProjectsProvider } from './WorkbenchProjectsProvider';
import {
  clearLocalSnapshots,
  readLocalSnapshot,
  setLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from '../lib/localSnapshots';

// Expanding a project draws its last stored first page of Sessions in the same
// render, and the live read then owns the window (docs/plans/web-ui-local-cache.md).
// The risks: an empty frame before the cached rows, a cached row surviving the
// live page that left it out, a later live page merged into cached rows, and a
// stored page outliving the Session, the project, or the reader it was stored for.

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const project: WorkbenchProject = {
  id: 'a',
  scope_id: 'avibe::project::a',
  display_name: 'a',
  folder_path: '/tmp/a',
  created_at: '2026-11-01T00:00:00Z',
  last_active_at: null,
  archived: false,
  default_agent: null,
  capabilities: { can_chat: true, has_folder: true },
};

const session = (id: string): WorkbenchSession => ({
  id,
  scope_id: 'avibe::project::a',
  project_id: 'a',
  title: id,
  agent_id: null,
  agent_name: 'vibey',
  agent_backend: 'vibey',
  agent_variant: null,
  model: null,
  reasoning_effort: null,
  status: 'active',
  visibility: 'foreground',
  pinned: false,
  agent_status: 'idle',
  workdir: null,
  native_session_id: null,
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:00:00Z',
  last_active_at: '2026-11-01T00:00:00Z',
  metadata: {},
} as WorkbenchSession);

const KEY = 'projects:sessions:a';
type Page = { sessions: WorkbenchSession[]; next_before_id: string | null };

async function storePage(reader: string, page: Page) {
  setLocalSnapshotPrincipal(reader);
  await writeLocalSnapshot(KEY, 1, page);
  setLocalSnapshotPrincipal(null);
}
const stored = () => readLocalSnapshot(KEY, 1, (value) => value as Page);
const ids = (rows: WorkbenchSession[] | null | undefined) => rows?.map((row) => row.id) ?? null;

type ListCall = { params: { projectId?: string; beforeId?: string }; resolve: (page: Page) => void; reject: (error: Error) => void };

function fakeApi(projects: WorkbenchProject[] = [project]) {
  const calls: ListCall[] = [];
  const handlers: WorkbenchEventHandlers[] = [];
  apiRef.current = {
    getWorkbenchProjectsBootstrap: vi.fn(async () => ({ projects, sessions: {} })),
    listSessions: vi.fn((params: ListCall['params']) => new Promise<Page>((resolve, reject) => {
      calls.push({ params, resolve, reject });
    })),
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  return {
    calls,
    handlers,
    answer: async (index: number, page: Page) => {
      await act(async () => { calls[index].resolve(page); });
      await settle();
    },
  };
}

// Every render the tree goes through, so a frame with an open project and no rows shows up.
function mount(reader: string, { active = true }: { active?: boolean } = {}) {
  const state = { current: null as WorkbenchProjectsTree | null, renders: [] as { open: boolean; rows: string[] | null }[] };
  const Probe = () => {
    const tree = useWorkbenchProjectsTree({ active });
    state.renders.push({ open: tree.isExpanded('a'), rows: ids(tree.sessionsOf('a').sessions) });
    useEffect(() => { state.current = tree; });
    return null;
  };
  const tree = (authorId: string) => (
    <WorkbenchProjectsProvider>
      <InstanceAuthorizationProvider session={{ remote: false, instance_kind: null, author_id: authorId }}>
        <Probe />
      </InstanceAuthorizationProvider>
    </WorkbenchProjectsProvider>
  );
  const view = render(tree(reader));
  return Object.assign(state, { switchReader: (authorId: string) => view.rerender(tree(authorId)) });
}

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
});

afterEach(async () => {
  cleanup();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('a project\'s stored Sessions', () => {
  it('are drawn in the same render that opens the project, before the live read answers', async () => {
    await storePage('local', { sessions: [session('s1'), session('s2')], next_before_id: 's2' });
    const api = fakeApi();
    const state = mount('local');
    await settle();

    state.renders.length = 0;
    act(() => state.current!.toggleExpanded('a'));
    expect(api.calls).toHaveLength(1);
    const open = state.renders.filter((frame) => frame.open);
    expect(open.length).toBeGreaterThan(0);
    expect(open.every((frame) => frame.rows?.join() === 's1,s2')).toBe(true);
    // Load more stays where it will be, and the live read is in flight.
    expect(state.current!.sessionsOf('a').cursor).toBe('s2');
    expect(state.current!.sessionsOf('a').loading).toBe(true);
  });

  it('are replaced by the live first page, which drops a Session the server left out, and stored as it', async () => {
    await storePage('local', { sessions: [session('s1'), session('gone')], next_before_id: null });
    const api = fakeApi();
    const state = mount('local');
    await settle();
    act(() => state.current!.toggleExpanded('a'));
    expect(ids(state.current!.sessionsOf('a').sessions)).toEqual(['s1', 'gone']);

    await api.answer(0, { sessions: [session('new'), session('s1')], next_before_id: null });
    expect(ids(state.current!.sessionsOf('a').sessions)).toEqual(['new', 's1']);
    expect(ids((await stored())?.sessions)).toEqual(['new', 's1']);
  });

  it('are not drawn when the stored page is empty: the empty state is the live answer\'s', async () => {
    await storePage('local', { sessions: [], next_before_id: null });
    const api = fakeApi();
    const state = mount('local');
    await settle();
    act(() => state.current!.toggleExpanded('a'));
    expect(state.current!.sessionsOf('a').sessions).toBeNull();
    expect(state.current!.sessionsOf('a').loading).toBe(true);
    await api.answer(0, { sessions: [session('s1')], next_before_id: null });
    expect(ids(state.current!.sessionsOf('a').sessions)).toEqual(['s1']);
  });

  it('lose a Session archived elsewhere: the stored page that lists it goes', async () => {
    const api = fakeApi();
    const state = mount('local');
    await settle();
    act(() => state.current!.toggleExpanded('a'));
    await api.answer(0, { sessions: [session('s1'), session('s2')], next_before_id: null });
    expect(ids((await stored())?.sessions)).toEqual(['s1', 's2']);

    await act(async () => {
      for (const handler of api.handlers) {
        handler.onSessionActivity?.({ session_id: 's2', scope_id: 'avibe::project::a', event: 'archived' });
      }
    });
    await settle();
    expect(await stored()).toBeNull();
  });

  it('go with a project the live list no longer has', async () => {
    await storePage('local', { sessions: [session('s1')], next_before_id: null });
    fakeApi([]);
    mount('local');
    await settle();
    setLocalSnapshotPrincipal('local');
    expect(await stored()).toBeNull();
  });

  it('painted for one reader go the moment another is confirmed, before any live answer', async () => {
    await storePage('remote:alice', { sessions: [session('alice_s1')], next_before_id: null });
    fakeApi();
    const state = mount('remote:alice');
    await settle();
    act(() => state.current!.toggleExpanded('a'));
    expect(ids(state.current!.sessionsOf('a').sessions)).toEqual(['alice_s1']);

    state.switchReader('remote:bob');
    await settle();
    expect(state.current!.sessionsOf('a').sessions).toBeNull();
    expect(state.current!.sessionsOf('a').loading).toBe(true);
  });

  it('do not change Load more: it pages on from the live cursor, and from painted rows it reads the first page', async () => {
    await storePage('local', { sessions: [session('s1'), session('s2')], next_before_id: 'cached-cursor' });
    const api = fakeApi();
    const state = mount('local');
    await settle();
    act(() => state.current!.toggleExpanded('a'));

    // The first live read fails: the painted rows stay, and their cursor is not
    // the server's, so Load more reads the first page instead of paging on.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    await act(async () => { api.calls[0].reject(new Error('offline')); });
    await settle();
    act(() => state.current!.loadMore('a'));
    expect(api.calls[1].params.beforeId).toBeUndefined();
    await api.answer(1, { sessions: [session('s3'), session('s1')], next_before_id: 'live-cursor' });
    expect(ids(state.current!.sessionsOf('a').sessions)).toEqual(['s3', 's1']);

    // From a live window, Load more pages on, and the stored first page stays as it was.
    act(() => state.current!.loadMore('a'));
    expect(api.calls[2].params.beforeId).toBe('live-cursor');
    await api.answer(2, { sessions: [session('s9')], next_before_id: null });
    expect(ids(state.current!.sessionsOf('a').sessions)).toEqual(['s3', 's1', 's9']);
    expect(ids((await stored())?.sessions)).toEqual(['s3', 's1']);
    expect((await stored())?.next_before_id).toBe('live-cursor');
    consoleError.mockRestore();
  });

  it('are stored as the first page of a wider window a reconnect re-reads, with the cursor after it', async () => {
    const api = fakeApi();
    const rows = Array.from({ length: 10 }, (_, index) => session(`s${index}`));
    const state = mount('local');
    await settle();
    act(() => state.current!.toggleExpanded('a'));
    await api.answer(0, { sessions: rows.slice(0, 8), next_before_id: 's7' });
    act(() => state.current!.loadMore('a'));
    await api.answer(1, { sessions: rows.slice(8), next_before_id: null });
    expect(state.current!.sessionsOf('a').sessions).toHaveLength(10);

    // A reconnect rebuilds the whole window in one bootstrap read.
    const bootstrap = (apiRef.current as { getWorkbenchProjectsBootstrap: ReturnType<typeof vi.fn> }).getWorkbenchProjectsBootstrap;
    bootstrap.mockImplementation(async (params?: { projectIds?: string[] }) => ({
      projects: [project],
      sessions: params?.projectIds ? { a: { sessions: [session('fresh'), ...rows.slice(0, 9)], next_before_id: null } } : {},
    }));
    await act(async () => { for (const handler of api.handlers) handler.onConnected?.(); });
    await settle();
    expect(state.current!.sessionsOf('a').sessions?.[0].id).toBe('fresh');
    const record = await stored();
    expect(ids(record?.sessions)).toEqual(['fresh', 's0', 's1', 's2', 's3', 's4', 's5', 's6']);
    expect(record?.next_before_id).toBe('s6');
  });

  it('lose a Session hidden while nothing has loaded them: storage is checked, not only memory', async () => {
    await storePage('local', { sessions: [session('s1'), session('hidden')], next_before_id: null });
    const api = fakeApi();
    // A page that reads no project (the phone's Inbox): nothing is loaded, and
    // the event names no project the tree knows.
    mount('local', { active: false });
    await settle();
    await act(async () => {
      for (const handler of api.handlers) {
        handler.onSessionActivity?.({ session_id: 'hidden', scope_id: null, event: 'updated', visibility: 'background' });
      }
    });
    await settle();
    setLocalSnapshotPrincipal('local');
    expect(await stored()).toBeNull();
  });
});
