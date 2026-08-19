// @vitest-environment jsdom

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkbenchProjectsProvider } from './WorkbenchProjectsProvider';
import { useWorkbenchProjectsTree } from './WorkbenchProjectsContext';
import type { ProjectDefaultAgent, WorkbenchEventHandlers } from './ApiContext';
import { resetCoalescedWrites } from '../lib/useCoalescedWrite';

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

// Records every PATCH and hands back a gate per call, so a write waiting behind
// the request in flight can be observed as "not sent yet" rather than merely
// "not resolved yet".
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
    // The writer's store is module state (a resource outlives the view that edits
    // it), so each case starts from an empty one.
    resetCoalescedWrites();
  });

  afterEach(() => {
    cleanup();
    apiRef.current = null;
    resetCoalescedWrites();
    vi.restoreAllMocks();
  });

  it('caches the pick before the request lands, and folds picks made during it into one follow-up', async () => {
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
      tree()!.setProjectDefaultAgent(project.id, route());
    });
    // The picker's highlight is this cache, so it has to be current already.
    expect(tree()!.projects?.[0].default_agent).toEqual(route());
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(true);
    expect(calls).toHaveLength(1);
    // The project held no route, and the compare-and-set token says what the
    // SERVER was last confirmed to hold — never what this cache now shows.
    expect(calls[0].payload).toMatchObject({ expected_agent_id: null, model: 'sonnet' });

    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }));
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'haiku' }));
    });
    expect(tree()!.projects?.[0].default_agent).toEqual(route({ model: 'haiku' }));
    // Still one request: a route is a whole-snapshot payload, so an earlier one
    // landing last would undo the newer pick.
    expect(calls).toHaveLength(1);

    await act(async () => {
      gates[0].resolve({ ...project, default_agent: route() });
      await gates[0].promise;
    });
    // Two picks, ONE follow-up request: the pick in between was transit, not
    // intent. Its token is the route the server has just confirmed.
    expect(calls).toHaveLength(2);
    expect(calls[1].payload).toMatchObject({ expected_agent_id: 'agt_claude', model: 'haiku' });
    // The first write's response is already outdated — installing it would drag
    // the row back to the model the user has clicked past.
    expect(tree()!.projects?.[0].default_agent).toEqual(route({ model: 'haiku' }));

    const serverRow = {
      ...project,
      display_name: 'Renamed by the server',
      default_agent: route({ model: 'haiku' }),
    };
    await act(async () => {
      gates[1].resolve(serverRow);
      await gates[1].promise;
    });
    await settle();
    // The last write's response IS the truth, including fields the client never sent.
    expect(tree()!.projects?.[0]).toEqual(serverRow);
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(false);
  });

  it('drops the pick waiting behind a rejected write, rolling the whole burst back', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockResolvedValueOnce({ projects: [project], sessions: {} })
      .mockResolvedValueOnce({ projects: [project], sessions: {} });
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
      tree()!.setProjectDefaultAgent(project.id, route());
    });
    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }));
    });
    expect(calls).toHaveLength(1);

    await act(async () => {
      gates[0].reject(new Error('boom'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // The waiting pick was composed against the route this request was
    // installing, and the server has just refused that route. Sending it would
    // persist a model chosen for an Agent the row never took, so it goes with
    // the burst.
    expect(calls).toHaveLength(1);
    // The rollback is the re-read, and it takes the burst back as a whole: the
    // user sees the row the server holds rather than a combination nobody picked.
    expect(getWorkbenchProjectsBootstrap).toHaveBeenNthCalledWith(2, { cache: false });
    expect(tree()!.projects?.[0].default_agent).toBeNull();
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(false);

    // Picking again after the rollback expects what that read found, not the
    // route the server refused.
    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }));
    });
    expect(calls).toHaveLength(2);
    expect(calls[1].payload).toMatchObject({ expected_agent_id: null, model: 'opus' });
  });

  it('does not let a rename response revert the route or poison the next write', async () => {
    const calls: UpdateCall[] = [];
    const gates: Deferred<unknown>[] = [];
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({ projects: [project], sessions: {} }),
      updateProject: gatedUpdateProject(calls, gates),
      connectWorkbenchEvents: connectWorkbenchEvents(),
    };
    const tree = renderTree();
    await settle();

    // A rename and a route pick are separate requests on the same row, and the
    // rename answers LAST — carrying the row as it was before the pick.
    act(() => {
      void tree()!.renameProject(project.id, 'Renamed');
    });
    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route());
    });
    expect(calls[0].payload).toEqual({ display_name: 'Renamed' });
    expect(calls[1].payload).toMatchObject({ expected_agent_id: null, model: 'sonnet' });

    await act(async () => {
      gates[1].resolve({ ...project, default_agent: route() });
      await gates[1].promise;
    });
    await settle();
    expect(tree()!.projects?.[0].default_agent).toEqual(route());

    await act(async () => {
      gates[0].resolve({ ...project, display_name: 'Renamed', default_agent: null });
      await gates[0].promise;
    });
    await settle();
    // Only the field the rename changed is taken. Installing its snapshot whole
    // would revert the route the user just set...
    expect(tree()!.projects?.[0].display_name).toBe('Renamed');
    expect(tree()!.projects?.[0].default_agent).toEqual(route());

    // ...and recording it as confirmed would hand the next pick a token naming a
    // route the server has already replaced — a deterministic conflict.
    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }));
    });
    expect(calls[2].payload).toMatchObject({ expected_agent_id: 'agt_claude', model: 'opus' });
  });

  it('rolls a rejected pick back by re-reading the server, and retries against that route', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const serverTruth = {
      ...project,
      default_agent: route({ agent_id: 'agt_codex', agent_name: 'codex', agent_variant: 'codex' }),
    };
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
      tree()!.setProjectDefaultAgent(project.id, route());
    });
    expect(tree()!.projects?.[0].default_agent).toEqual(route());

    await act(async () => {
      gates[0].reject(new Error('project_agent_conflict'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // The pick lived only in this cache, so the rollback is a re-read — which
    // here reveals the route another surface had set meanwhile.
    expect(getWorkbenchProjectsBootstrap).toHaveBeenNthCalledWith(2, { cache: false });
    expect(tree()!.projects?.[0].default_agent).toEqual(serverTruth.default_agent);
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(false);

    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }));
    });
    // A read confirms a route too: the next write expects what that read found.
    expect(calls).toHaveLength(2);
    expect(calls[1].payload).toMatchObject({ expected_agent_id: 'agt_codex', model: 'opus' });
  });

  it('keeps each project on its own writer, so one failing project cannot hold another up', async () => {
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
      tree()!.setProjectDefaultAgent(project.id, route());
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }));
      tree()!.setProjectDefaultAgent(other.id, route({ model: 'sonnet' }));
    });
    // Two requests, not one: Project B has nothing to overwrite in Project A, so
    // it goes out immediately instead of waiting behind A's request.
    expect(calls.map((c) => c.projectId)).toEqual([project.id, other.id]);

    await act(async () => {
      gates[1].resolve(otherSaved);
      await gates[1].promise;
    });
    await settle();
    // B settles on its own: no rollback read, and no waiting on A.
    expect(tree()!.isSavingDefaultAgent(other.id)).toBe(false);
    expect(getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(1);

    await act(async () => {
      gates[0].reject(new Error('project_agent_conflict'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // A's failure ends A's burst, taking the pick that was waiting behind it with
    // it — so there is no third request — and A rolls back to the server route.
    // B's pick survives a failure it had no part in.
    expect(calls.map((c) => c.projectId)).toEqual([project.id, other.id]);
    expect(tree()!.projects?.[0].default_agent).toBeNull();
    expect(tree()!.projects?.[1].default_agent).toEqual(route({ model: 'sonnet' }));
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(false);
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
      tree()!.setProjectDefaultAgent(project.id, cleared);
    });

    // An all-null route means "follow the global default", which the server
    // reports as no route at all — consumers read `default_agent?.agent_name`.
    expect(tree()!.projects?.[0].default_agent).toBeNull();
    expect(calls[0].payload).toMatchObject({
      agent_id: null,
      agent_name: null,
      model: null,
      // The bootstrap is a confirmation: clearing expects the route it reported.
      expected_agent_id: 'agt_claude',
    });
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
      tree()!.setProjectDefaultAgent(project.id, route());
    });

    await act(async () => {
      stale.resolve({ projects: [project], sessions: {} });
      await stale.promise;
    });

    // That read began before the pick, so its snapshot is the pre-pick route.
    expect(tree()!.projects?.[0].default_agent).toEqual(route());
  });

  it('keeps a read that starts after the pick from undoing it, while still installing the rest of that snapshot', async () => {
    const later = deferred<unknown>();
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockResolvedValueOnce({ projects: [project], sessions: {} })
      .mockReturnValueOnce(later.promise)
      .mockResolvedValueOnce({
        projects: [{ ...project, default_agent: route({ model: 'opus' }) }],
        sessions: {},
      });
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
      tree()!.setProjectDefaultAgent(project.id, route());
    });
    // A read issued AFTER the pick: its stamp is legitimately current, so the
    // ordering fence cannot refuse it — yet the server had not committed the
    // PATCH when it was answered.
    act(() => {
      void tree()!.refreshProjects();
    });
    await act(async () => {
      later.resolve({
        projects: [{ ...project, display_name: 'Renamed elsewhere', default_agent: null }],
        sessions: {},
      });
      await later.promise;
    });
    await settle();

    // The pick outranks the incoming row, and only on that one field: this
    // snapshot is otherwise the newest truth there is, so discarding it whole
    // would drop a rename the user is entitled to see.
    expect(tree()!.projects?.[0].default_agent).toEqual(route());
    expect(tree()!.projects?.[0].display_name).toBe('Renamed elsewhere');

    await act(async () => {
      gates[0].resolve({ ...project, display_name: 'Renamed elsewhere', default_agent: route() });
      await gates[0].promise;
    });
    await settle();
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(false);

    // And the precedence is not sticky: once the write has settled there is no
    // pick to protect, so a later read is free to move the route again — which is
    // how another surface's change reaches this one.
    await act(async () => {
      await tree()!.refreshProjects();
    });
    await settle();
    expect(tree()!.projects?.[0].default_agent).toEqual(route({ model: 'opus' }));
  });

  it('reverts a rejected burst to the route it replaced, without waiting for a re-read', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const stuck = deferred<unknown>();
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockResolvedValueOnce({ projects: [{ ...project, default_agent: route() }], sessions: {} })
      .mockReturnValueOnce(stuck.promise);
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
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'opus' }));
    });
    expect(tree()!.projects?.[0].default_agent).toEqual(route({ model: 'opus' }));

    await act(async () => {
      gates[0].reject(new Error('boom'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // The rollback is local and complete on its own: the pick lived only in this
    // cache, so putting back the route it replaced needs no network. The re-read
    // below may only be QUEUED — ``fetchProjects`` records a trailing intent when
    // one is already in flight — so a rollback that waited for it would clear the
    // indicator while leaving the refused route on screen.
    expect(tree()!.projects?.[0].default_agent).toEqual(route());
    expect(tree()!.isSavingDefaultAgent(project.id)).toBe(false);
    // Still asked, unawaited: a compare-and-set conflict means someone else moved
    // the route, and only a read can say to what.
    expect(getWorkbenchProjectsBootstrap).toHaveBeenNthCalledWith(2, { cache: false });

    // The revert restores what the server CONFIRMED, so the next pick's token is
    // coherent with it even though that read has not answered.
    act(() => {
      tree()!.setProjectDefaultAgent(project.id, route({ model: 'haiku' }));
    });
    expect(calls).toHaveLength(2);
    expect(calls[1].payload).toMatchObject({ expected_agent_id: 'agt_claude', model: 'haiku' });
  });
});
