// @vitest-environment jsdom

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkbenchProjectsProvider } from './WorkbenchProjectsProvider';
import { useWorkbenchProjectsTree } from './WorkbenchProjectsContext';
import type { ProjectDefaultAgent, WorkbenchEventHandlers } from './ApiContext';

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function settle() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

const project = {
  id: 'proj_a',
  scope_id: 'scope_a',
  display_name: 'Project A',
  folder_path: '/tmp/project-a',
  created_at: '2026-08-19T00:00:00Z',
  last_active_at: null,
  archived: false,
  default_agent: null,
};

const route = (over: Partial<ProjectDefaultAgent> = {}): ProjectDefaultAgent => ({
  agent_backend: null,
  agent_id: 'agt_claude',
  agent_name: 'claude',
  agent_variant: 'claude',
  model: 'sonnet',
  reasoning_effort: null,
  ...over,
});

type UpdateCall = { projectId: string; payload: Record<string, unknown> };

type FakeApi = {
  getWorkbenchProjectsBootstrap?: (params?: { cache?: boolean }) => Promise<unknown>;
  updateProject?: (projectId: string, payload: Record<string, unknown>) => Promise<unknown>;
  connectWorkbenchEvents: (handlers: WorkbenchEventHandlers) => () => void;
};

const apiRef = { current: null as FakeApi | null };

vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

const connectWorkbenchEvents = () => vi.fn(() => vi.fn());

// Records every PATCH and hands back a gate per call, so a queued write can be
// observed as "not sent yet" rather than merely "not resolved yet".
function gatedUpdateProject(calls: UpdateCall[], gates: Deferred<unknown>[]) {
  return vi.fn((projectId: string, payload: Record<string, unknown>) => {
    calls.push({ projectId, payload });
    const gate = deferred<unknown>();
    gates.push(gate);
    return gate.promise;
  });
}

function renderTree() {
  let tree: ReturnType<typeof useWorkbenchProjectsTree> | null = null;
  const Probe = () => {
    const value = useWorkbenchProjectsTree();
    useEffect(() => {
      tree = value;
    }, [value]);
    return null;
  };
  render(
    <WorkbenchProjectsProvider>
      <Probe />
    </WorkbenchProjectsProvider>,
  );
  return () => tree;
}

describe('project default Agent route', () => {
  beforeEach(() => {
    apiRef.current = null;
  });

  afterEach(() => {
    cleanup();
    apiRef.current = null;
    vi.restoreAllMocks();
  });

  it('caches the pick before the request lands, and sends queued picks in click order', async () => {
    const calls: UpdateCall[] = [];
    const gates: Deferred<unknown>[] = [];
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({ projects: [project], sessions: {} }),
      updateProject: gatedUpdateProject(calls, gates),
      connectWorkbenchEvents: connectWorkbenchEvents(),
    };
    const tree = renderTree();
    await settle();

    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route(), null);
    });
    // The picker's highlight is this cache, so it has to be current already.
    expect(tree()!.projects?.[0].default_agent).toEqual(route());
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(true);
    expect(calls).toHaveLength(1);

    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }), 'agt_claude');
    });
    expect(tree()!.projects?.[0].default_agent).toEqual(route({ model: 'opus' }));
    // Still one request: `expected_agent_id` is a compare-and-set against what
    // the write ahead installs, so it cannot be sent concurrently with it.
    expect(calls).toHaveLength(1);

    await act(async () => {
      gates[0].resolve({ ...project, default_agent: route() });
      await gates[0].promise;
    });
    expect(calls).toHaveLength(2);
    expect(calls[1].payload).toMatchObject({ expected_agent_id: 'agt_claude', model: 'opus' });
    // The first write's response is already outdated — installing it would drag
    // the row back to the model the user has clicked past.
    expect(tree()!.projects?.[0].default_agent).toEqual(route({ model: 'opus' }));

    const serverRow = { ...project, display_name: 'Renamed by the server', default_agent: route({ model: 'opus' }) };
    await act(async () => {
      gates[1].resolve(serverRow);
      await gates[1].promise;
    });
    await settle();
    // The last write's response IS the truth, including fields the client never sent.
    expect(tree()!.projects?.[0]).toEqual(serverRow);
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(false);
  });

  it('rolls a rejected pick back by re-reading the server, and drops the writes behind it', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const serverTruth = { ...project, default_agent: null };
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockResolvedValueOnce({ projects: [project], sessions: {} })
      .mockResolvedValueOnce({ projects: [serverTruth], sessions: {} });
    const calls: UpdateCall[] = [];
    const gates: Deferred<unknown>[] = [];
    apiRef.current = {
      getWorkbenchProjectsBootstrap,
      updateProject: gatedUpdateProject(calls, gates),
      connectWorkbenchEvents: connectWorkbenchEvents(),
    };
    const tree = renderTree();
    await settle();

    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route(), null);
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }), 'agt_claude');
    });
    expect(tree()!.projects?.[0].default_agent).toEqual(route({ model: 'opus' }));

    await act(async () => {
      gates[0].reject(new Error('project_agent_conflict'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // The queued write was built on a route the server never took.
    expect(calls).toHaveLength(1);
    expect(getWorkbenchProjectsBootstrap).toHaveBeenNthCalledWith(2, { cache: false });
    expect(tree()!.projects?.[0].default_agent).toBeNull();
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(false);
  });

  it('keeps each project on its own queue, so one failing project cannot discard another', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const other = { ...project, id: 'proj_b', scope_id: 'scope_b', display_name: 'Project B' };
    const otherSaved = { ...other, default_agent: route({ model: 'sonnet' }) };
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockResolvedValueOnce({ projects: [project, other], sessions: {} })
      // The rollback re-read is a whole-tree read: by then the server HAS taken
      // Project B's write, so its row comes back with B's route, not the pre-pick one.
      .mockResolvedValueOnce({ projects: [{ ...project, default_agent: null }, otherSaved], sessions: {} });
    const calls: UpdateCall[] = [];
    const gates: Deferred<unknown>[] = [];
    apiRef.current = {
      getWorkbenchProjectsBootstrap,
      updateProject: gatedUpdateProject(calls, gates),
      connectWorkbenchEvents: connectWorkbenchEvents(),
    };
    const tree = renderTree();
    await settle();

    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route(), null);
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }), 'agt_claude');
      tree()!.setProjectDefaultAgent(other.id, route({ model: 'sonnet' }), null);
    });
    // Two requests, not one: Project B has nothing to overwrite in Project A, so
    // it goes out immediately instead of waiting behind A's queue.
    expect(calls.map((c) => c.projectId)).toEqual([project.id, other.id]);

    await act(async () => {
      gates[1].resolve(otherSaved);
      await gates[1].promise;
    });
    await act(async () => {
      gates[0].reject(new Error('project_agent_conflict'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // A's queued write is dropped and A rolls back; B's pick is untouched by a
    // failure it had no part in.
    expect(calls).toHaveLength(2);
    expect(tree()!.projects?.[0].default_agent).toBeNull();
    expect(tree()!.projects?.[1].default_agent).toEqual(route({ model: 'sonnet' }));
    expect(tree()!.isSavingDefaultAgent(other.id)).toBe(false);
  });

  it('caches a cleared route as no default at all', async () => {
    const calls: UpdateCall[] = [];
    const gates: Deferred<unknown>[] = [];
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({
        projects: [{ ...project, default_agent: route() }],
        sessions: {},
      }),
      updateProject: gatedUpdateProject(calls, gates),
      connectWorkbenchEvents: connectWorkbenchEvents(),
    };
    const tree = renderTree();
    await settle();

    const cleared = route({ agent_id: null, agent_name: null, agent_variant: null, model: null });
    act(() => {
      tree()!.setProjectDefaultAgent(project.id, cleared, 'agt_claude');
    });

    // An all-null route means "follow the global default", which the server
    // reports as no route at all — consumers read `default_agent?.agent_name`.
    expect(tree()!.projects?.[0].default_agent).toBeNull();
    expect(calls[0].payload).toMatchObject({ agent_id: null, agent_name: null, model: null });
  });

  it('keeps a projects read that was already in flight from undoing the pick', async () => {
    const stale = deferred<unknown>();
    const neverSettles = deferred<unknown>();
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockResolvedValueOnce({ projects: [project], sessions: {} })
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(neverSettles.promise);
    const calls: UpdateCall[] = [];
    const gates: Deferred<unknown>[] = [];
    apiRef.current = {
      getWorkbenchProjectsBootstrap,
      updateProject: gatedUpdateProject(calls, gates),
      connectWorkbenchEvents: connectWorkbenchEvents(),
    };
    const tree = renderTree();
    await settle();

    act(() => {
      void tree()!.refreshProjects();
    });
    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route(), null);
    });

    await act(async () => {
      stale.resolve({ projects: [project], sessions: {} });
      await stale.promise;
    });

    // That read began before the pick, so its snapshot is the pre-pick route.
    expect(tree()!.projects?.[0].default_agent).toEqual(route());
  });
});
