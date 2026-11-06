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
  forgetLocalSnapshots,
  setLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from '../lib/localSnapshots';
import { readExpandedProjects, rememberExpandedProjects } from '../lib/projectExpansionSnapshot';

// The project list comes back the way the reader left it: the projects they had
// open are stored for them on this device, and open again in the render that
// draws the list, with the rows they had (docs/plans/web-ui-local-cache.md). The
// risks: a frame with the list drawn and a restored project closed, a toggle a
// reload loses, the open set of one reader shown to another, and a set that
// outlives the authorization or the projects it was stored under.

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const projectOf = (id: string): WorkbenchProject => ({
  id,
  scope_id: `avibe::project::${id}`,
  display_name: id,
  folder_path: `/tmp/${id}`,
  created_at: '2026-11-01T00:00:00Z',
  last_active_at: null,
  archived: false,
  default_agent: null,
  capabilities: { can_chat: true, has_folder: true },
});

const session = (id: string, projectId = 'a'): WorkbenchSession => ({
  id,
  scope_id: `avibe::project::${projectId}`,
  project_id: projectId,
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

type Page = { sessions: WorkbenchSession[]; next_before_id: string | null };
type BootstrapParams = { projectIds?: string[]; status?: string; limit?: number; cache?: boolean };

/** Stores, as ``reader``, what an earlier visit left: open projects, a painted list, stored pages. */
async function storeForReader(reader: string, stored: { open?: string[]; list?: string[]; pages?: Record<string, Page> }) {
  setLocalSnapshotPrincipal(reader);
  if (stored.open) await rememberExpandedProjects(stored.open);
  if (stored.list) await writeLocalSnapshot('projects:bootstrap', 1, { projects: stored.list.map(projectOf) });
  for (const [projectId, page] of Object.entries(stored.pages ?? {})) {
    await writeLocalSnapshot(`projects:sessions:${projectId}`, 1, page);
  }
  setLocalSnapshotPrincipal(null);
}

/** The open projects stored for ``reader``. */
async function storedOpen(reader: string): Promise<string[]> {
  setLocalSnapshotPrincipal(reader);
  return readExpandedProjects();
}

const pageOf = (projectId: string) => ({ sessions: [session(`${projectId}-live`, projectId)], next_before_id: null });

/** A server holding ``listed``. ``hold`` keeps every bootstrap unanswered; ``fail`` refuses them. */
function fakeApi(listed: string[], { hold = false, fail = false }: { hold?: boolean; fail?: boolean } = {}) {
  const handlers: WorkbenchEventHandlers[] = [];
  const bootstraps: (BootstrapParams | undefined)[] = [];
  const server = { listed };
  const held: (() => void)[] = [];
  const listSessions = vi.fn(async ({ projectId }: { projectId: string }) => pageOf(projectId));
  apiRef.current = {
    getWorkbenchProjectsBootstrap: vi.fn((params?: BootstrapParams) => {
      bootstraps.push(params);
      const answer = () => {
        const projects = server.listed.map(projectOf);
        const sessions = Object.fromEntries((params?.projectIds ?? [])
          .filter((projectId) => server.listed.includes(projectId))
          .map((projectId) => [projectId, pageOf(projectId)]));
        return { projects, sessions };
      };
      if (hold) return new Promise((resolve) => { held.push(() => resolve(answer())); });
      if (fail) return Promise.reject(new Error('offline'));
      return Promise.resolve(answer());
    }),
    listSessions,
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  return {
    handlers, bootstraps, listSessions, server,
    /** Answer the bootstraps held so far, with what the server holds now. */
    release: async () => {
      await act(async () => { for (const answer of held.splice(0)) answer(); });
      await settle();
    },
  };
}

type Frame = { listed: string[] | null; open: string[]; rows: Record<string, string[] | null> };

// Every render the tree goes through, so a frame with the list drawn and a
// restored project closed shows up.
function mount(reader: string) {
  const state = { current: null as WorkbenchProjectsTree | null, frames: [] as Frame[] };
  const Probe = () => {
    const tree = useWorkbenchProjectsTree({ active: true });
    const listed = tree.projects?.map((project) => project.id) ?? null;
    state.frames.push({
      listed,
      open: (listed ?? []).filter((projectId) => tree.isExpanded(projectId)),
      rows: Object.fromEntries((listed ?? []).map((projectId) => [projectId, tree.sessionsOf(projectId).sessions?.map((row) => row.id) ?? null])),
    });
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
const listedFrames = (frames: Frame[]) => frames.filter((frame) => frame.listed !== null && frame.listed.length > 0);

afterEach(async () => {
  cleanup();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
  window.localStorage.clear();
});

describe('the projects a reader leaves open', () => {
  it('are stored at every expand and collapse, down to none', async () => {
    fakeApi(['a', 'b']);
    const state = mount('local');
    await settle();
    const toggle = async (projectId: string) => {
      act(() => state.current!.toggleExpanded(projectId));
      await settle();
    };
    await toggle('a');
    expect(await storedOpen('local')).toEqual(['a']);
    setLocalSnapshotPrincipal('local');
    await toggle('b');
    expect(await storedOpen('local')).toEqual(['a', 'b']);
    setLocalSnapshotPrincipal('local');
    await toggle('a');
    expect(await storedOpen('local')).toEqual(['b']);
    setLocalSnapshotPrincipal('local');
    await toggle('b');
    expect(await storedOpen('local')).toEqual([]);
  });

  it('open in the same render as the painted list, with their stored rows, before any live answer', async () => {
    await storeForReader('local', {
      open: ['a'],
      list: ['a', 'b'],
      pages: { a: { sessions: [session('s1'), session('s2')], next_before_id: 's2' } },
    });
    const api = fakeApi(['a', 'b'], { hold: true });
    const state = mount('local');
    await settle();

    const frames = listedFrames(state.frames);
    expect(frames.length).toBeGreaterThan(0);
    // No frame draws the list with the project closed, or open without its rows.
    expect(frames.every((frame) => frame.open.join() === 'a' && frame.rows.a?.join() === 's1,s2')).toBe(true);
    // The first live list asks for that project's first page in the same request.
    expect(api.bootstraps[0]).toMatchObject({ projectIds: ['a'], status: 'active', limit: 8 });
  });

  it('open in the same render as the first live list when none was painted, with the page that list brought', async () => {
    await storeForReader('local', { open: ['b'] });
    const api = fakeApi(['a', 'b']);
    const state = mount('local');
    await settle();

    const frames = listedFrames(state.frames);
    expect(frames.length).toBeGreaterThan(0);
    expect(frames.every((frame) => frame.open.join() === 'b')).toBe(true);
    expect(state.current!.sessionsOf('b').sessions?.map((row) => row.id)).toEqual(['b-live']);
    expect(api.bootstraps).toHaveLength(1);
    expect(api.bootstraps[0]).toMatchObject({ projectIds: ['b'], status: 'active', limit: 8 });
    // The page came with the list: no read of its own.
    expect(api.listSessions).not.toHaveBeenCalled();
  });

  it('read their own page when the list could not bring it, so a failure shows its retry rather than a skeleton', async () => {
    await storeForReader('local', { open: ['a'], list: ['a'] });
    const api = fakeApi(['a'], { fail: true });
    const state = mount('local');
    await settle();

    expect(state.current!.isExpanded('a')).toBe(true);
    expect(api.listSessions).toHaveBeenCalledWith(expect.objectContaining({ projectId: 'a' }));
    expect(state.current!.sessionsOf('a').loading).toBe(false);
    expect(state.current!.sessionsOf('a').sessions?.map((row) => row.id)).toEqual(['a-live']);
  });

  it('wait for the live list to open one the painted list is too old to have, and keep it stored meanwhile', async () => {
    // Created just before the last reload, whose bootstrap failed: open, but not
    // in the painted list yet.
    await storeForReader('local', { open: ['a', 'new'], list: ['a'] });
    const api = fakeApi(['a', 'new'], { hold: true });
    const state = mount('local');
    await settle();
    expect(state.current!.isExpanded('a')).toBe(true);
    expect(await storedOpen('local')).toEqual(['a', 'new']);
    // A toggle meanwhile stores it with the rest, not over it.
    setLocalSnapshotPrincipal('local');
    act(() => state.current!.toggleExpanded('a'));
    await settle();
    expect(await storedOpen('local')).toEqual(['new']);

    setLocalSnapshotPrincipal('local');
    await api.release();
    expect(state.current!.projects?.map((project) => project.id)).toEqual(['a', 'new']);
    expect(state.current!.isExpanded('new')).toBe(true);
    expect(state.current!.sessionsOf('new').sessions?.map((row) => row.id)).toEqual(['new-live']);
    expect(await storedOpen('local')).toEqual(['new']);
  });

  it('lose a project the live list no longer has, on screen and in the record', async () => {
    await storeForReader('local', { open: ['a', 'gone'] });
    const api = fakeApi(['a']);
    const state = mount('local');
    await settle();
    expect(state.current!.isExpanded('a')).toBe(true);
    expect(state.current!.isExpanded('gone')).toBe(false);
    expect(await storedOpen('local')).toEqual(['a']);

    // The project goes elsewhere: the next live list leaves it out.
    setLocalSnapshotPrincipal('local');
    api.server.listed = ['b'];
    await act(async () => {
      for (const handler of api.handlers) handler.onProjectsChanged?.({});
    });
    await settle();
    expect(state.current!.isExpanded('a')).toBe(false);
    expect(await storedOpen('local')).toEqual([]);
  });

  it('are this reader\'s alone: another reader on the device opens none of them', async () => {
    await storeForReader('alice', { open: ['a'] });
    const api = fakeApi(['a', 'b']);
    const state = mount('bob');
    await settle();
    expect(listedFrames(state.frames).every((frame) => frame.open.length === 0)).toBe(true);
    expect(api.bootstraps[0]?.projectIds).toBeUndefined();
  });

  it('close when another reader is named in the same document, who then keeps their own', async () => {
    await storeForReader('alice', { open: ['a'] });
    fakeApi(['a', 'b']);
    const state = mount('alice');
    await settle();
    expect(state.current!.isExpanded('a')).toBe(true);

    state.switchReader('bob');
    await settle();
    expect(state.current!.isExpanded('a')).toBe(false);
    act(() => state.current!.toggleExpanded('b'));
    await settle();
    expect(await storedOpen('bob')).toEqual(['b']);
  });

  it('are gone after an authorization change, and a later visit opens nothing', async () => {
    await storeForReader('local', { open: ['a'] });
    const api = fakeApi(['a', 'b']);
    const state = mount('local');
    await settle();
    expect(state.current!.isExpanded('a')).toBe(true);

    // What ``applyAuthorizationChange`` does: clear the store, then tell every surface.
    await act(async () => {
      void clearLocalSnapshots();
      for (const handler of api.handlers) handler.onAuthorizationChanged?.({});
    });
    await settle();
    expect(state.current!.isExpanded('a')).toBe(false);
    expect(await storedOpen('local')).toEqual([]);

    cleanup();
    setLocalSnapshotPrincipal(null);
    const again = mount('local');
    await settle();
    expect(again.current!.isExpanded('a')).toBe(false);
  });

  it('are gone after an authorization change that lands while one still waits for the live list', async () => {
    await storeForReader('local', { open: ['a', 'new'], list: ['a'] });
    const api = fakeApi(['a', 'new'], { hold: true });
    const state = mount('local');
    await settle();
    expect(await storedOpen('local')).toEqual(['a', 'new']);

    setLocalSnapshotPrincipal('local');
    await act(async () => {
      void clearLocalSnapshots();
      for (const handler of api.handlers) handler.onAuthorizationChanged?.({});
    });
    await settle();
    expect(state.current!.isExpanded('a')).toBe(false);
    expect(await storedOpen('local')).toEqual([]);
    setLocalSnapshotPrincipal('local');
    await api.release();
    expect(state.current!.isExpanded('new')).toBe(false);
  });

  it('are gone after an authorization change that lands before any list could open them', async () => {
    // Nothing painted, and the live list is slow: the stored set has been read
    // but no list has opened it yet.
    await storeForReader('local', { open: ['a'] });
    const api = fakeApi(['a', 'b'], { hold: true });
    const state = mount('local');
    await settle();
    expect(state.current!.projects).toBeNull();

    await act(async () => {
      void clearLocalSnapshots();
      for (const handler of api.handlers) handler.onAuthorizationChanged?.({});
    });
    await settle();
    await api.release();
    await api.release();
    expect(state.current!.projects?.map((project) => project.id)).toEqual(['a', 'b']);
    expect(state.current!.isExpanded('a')).toBe(false);
    expect(await storedOpen('local')).toEqual([]);
  });

  it('are gone after sign-out', async () => {
    await storeForReader('local', { open: ['a'] });
    setLocalSnapshotPrincipal('local');
    await forgetLocalSnapshots();
    fakeApi(['a']);
    const state = mount('local');
    await settle();
    expect(state.current!.isExpanded('a')).toBe(false);
  });
});
