// @vitest-environment jsdom

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkbenchInboxProvider } from './WorkbenchInboxProvider';
import { useWorkbenchInbox } from './WorkbenchInboxContext';
import { WorkbenchProjectsProvider } from './WorkbenchProjectsProvider';
import { useWorkbenchProjectsTree } from './WorkbenchProjectsContext';
import type { WorkbenchEventHandlers } from './ApiContext';

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

const session = {
  id: 'ses_a',
  scope_id: 'scope_a',
  project_id: 'proj_a',
  title: 'Visible before hide',
  agent_id: null,
  agent_name: null,
  agent_backend: 'codex',
  agent_variant: null,
  model: null,
  reasoning_effort: null,
  status: 'active',
  visibility: 'foreground' as const,
  pinned: false,
  agent_status: 'idle' as const,
  workdir: null,
  native_session_id: null,
  created_at: '2026-08-11T00:00:00Z',
  updated_at: '2026-08-11T00:00:00Z',
  last_active_at: '2026-08-11T00:00:00Z',
  metadata: {},
};

const project = {
  id: 'proj_a',
  scope_id: 'scope_a',
  display_name: 'Project A',
  folder_path: '/tmp/project-a',
  created_at: '2026-08-11T00:00:00Z',
  last_active_at: null,
  archived: false,
};

const projectB = {
  ...project,
  id: 'proj_b',
  scope_id: 'scope_b',
  display_name: 'Project B',
  folder_path: '/tmp/project-b',
};

const sessionB = {
  ...session,
  id: 'ses_b',
  scope_id: projectB.scope_id,
  project_id: projectB.id,
  title: 'Project B session',
};

const inboxRow = {
  session_id: session.id,
  scope_id: session.scope_id,
  title: session.title,
  preview: 'A stale inbox snapshot',
  preview_message_id: 'msg_a',
  last_activity_at: session.last_active_at,
  unread_count: 3,
  visibility: 'foreground' as const,
};

type FakeApi = {
  getWorkbenchProjectsBootstrap?: () => Promise<unknown>;
  listSessions?: (args: { projectId: string }) => Promise<unknown>;
  getSession?: () => Promise<unknown>;
  listInbox?: () => Promise<unknown>;
  markSessionRead?: () => Promise<unknown>;
  connectWorkbenchEvents: (handlers: WorkbenchEventHandlers) => () => void;
};

const apiRef = { current: null as FakeApi | null };

vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

function settle() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('Workbench session read ownership', () => {
  beforeEach(() => {
    apiRef.current = null;
  });

  afterEach(() => {
    cleanup();
    apiRef.current = null;
  });

  it('does not let a bootstrap issued before hide repopulate the projects tree', async () => {
    const staleBootstrap = deferred({ projects: [project], sessions: { proj_a: { sessions: [session], next_before_id: null } } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn()
        .mockResolvedValueOnce({ projects: [project], sessions: { proj_a: { sessions: [session], next_before_id: null } } })
        .mockReturnValueOnce(staleBootstrap.promise),
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();

    act(() => {
      handlers?.onConnected?.({ sub_id: 1, source: 'browser' });
    });
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'archived',
      });
    });
    await act(async () => {
      staleBootstrap.resolve({ projects: [project], sessions: { proj_a: { sessions: [session], next_before_id: null } } });
      await staleBootstrap.promise;
    });
    await settle();

    expect(tree?.projects).toEqual([project]);
    expect(tree?.sessionsOf('proj_a').sessions).toEqual([]);
  });

  it('retries a cold project bootstrap invalidated by activity', async () => {
    const staleBootstrap = deferred({ projects: [project], sessions: { proj_a: { sessions: [session], next_before_id: null } } });
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockReturnValueOnce(staleBootstrap.promise)
      .mockResolvedValueOnce({ projects: [project], sessions: { proj_a: { sessions: [session], next_before_id: null } } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();

    act(() => {
      handlers?.onSessionStatus?.({ session_id: session.id, agent_status: 'idle' });
    });
    await act(async () => {
      staleBootstrap.resolve({ projects: [project], sessions: { proj_a: { sessions: [session], next_before_id: null } } });
      await staleBootstrap.promise;
    });
    await settle();

    expect(getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(2);
    expect(tree?.projects).toEqual([project]);
    expect(tree?.sessionsOf(project.id).sessions).toEqual([session]);
  });

  it('serializes the initial project bootstrap with reconnect recovery', async () => {
    const initialBootstrap = deferred({
      projects: [project],
      sessions: { [project.id]: { sessions: [session], next_before_id: null } },
    });
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockReturnValueOnce(initialBootstrap.promise)
      .mockRejectedValueOnce(new Error('later reconnect failed'));
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();
    act(() => {
      handlers?.onConnected?.({ sub_id: 2, source: 'browser' });
    });
    await settle();
    expect(getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(1);
    await act(async () => {
      initialBootstrap.resolve({
        projects: [project],
        sessions: { [project.id]: { sessions: [session], next_before_id: null } },
      });
      await initialBootstrap.promise;
    });
    await settle();

    expect(getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(2);
    expect(tree?.projects).toEqual([project]);
    expect(tree?.sessionsOf(project.id).sessions).toEqual([session]);
  });

  it('retries a cold project list invalidated by a local project upsert', async () => {
    const staleBootstrap = deferred({
      projects: [project, projectB],
      sessions: { [project.id]: { sessions: [session], next_before_id: null } },
    });
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockReturnValueOnce(staleBootstrap.promise)
      .mockResolvedValueOnce({
        projects: [project, projectB],
        sessions: { [project.id]: { sessions: [session], next_before_id: null } },
      });
    apiRef.current = {
      getWorkbenchProjectsBootstrap,
      listSessions: vi.fn().mockResolvedValue({ sessions: [session], next_before_id: null }),
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
      createProject: vi.fn().mockResolvedValue(project),
    };
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
    await settle();
    await act(async () => {
      await tree?.createProject({ folder_path: '/tmp/a' });
    });
    await settle();
    await act(async () => {
      staleBootstrap.resolve({
        projects: [project, projectB],
        sessions: { [project.id]: { sessions: [session], next_before_id: null } },
      });
      await staleBootstrap.promise;
    });
    await settle();

    expect(getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(2);
    expect(tree?.projects).toEqual([project, projectB]);
  });

  it('retries reconnect project recovery invalidated by a local project upsert', async () => {
    const recoveredProject = { ...project, display_name: 'Project A from recovery' };
    const localProject = { ...project, display_name: 'Project A opened locally' };
    const staleReconnect = deferred({
      projects: [recoveredProject, projectB],
      sessions: { [project.id]: { sessions: [session], next_before_id: null } },
    });
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockResolvedValueOnce({
        projects: [project],
        sessions: { [project.id]: { sessions: [session], next_before_id: null } },
      })
      .mockReturnValueOnce(staleReconnect.promise)
      .mockResolvedValueOnce({
        projects: [localProject, projectB],
        sessions: { [project.id]: { sessions: [session], next_before_id: null } },
      });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap,
      createProject: vi.fn().mockResolvedValue(localProject),
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();
    act(() => {
      handlers?.onConnected?.({ sub_id: 3, source: 'browser' });
    });
    await settle();
    expect(getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(2);
    await act(async () => {
      await tree?.createProject({ folder_path: '/tmp/local' });
    });
    await act(async () => {
      staleReconnect.resolve({
        projects: [recoveredProject, projectB],
        sessions: { [project.id]: { sessions: [session], next_before_id: null } },
      });
      await staleReconnect.promise;
    });
    await settle();

    expect(getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(3);
    expect(tree?.projects).toEqual([localProject, projectB]);
    expect(tree?.sessionsOf(project.id).sessions).toEqual([session]);
  });

  it('retries an expanded project first-page read invalidated by activity', async () => {
    const staleFirstPage = deferred({ sessions: [session], next_before_id: null });
    const listSessions = vi.fn()
      .mockReturnValueOnce(staleFirstPage.promise)
      .mockResolvedValueOnce({ sessions: [session], next_before_id: null });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({ projects: [project], sessions: {} }),
      listSessions,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();
    act(() => {
      tree?.toggleExpanded(project.id);
    });
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        title: 'Fresh title',
      });
    });
    await act(async () => {
      staleFirstPage.resolve({ sessions: [session], next_before_id: null });
      await staleFirstPage.promise;
    });
    await settle();

    expect(listSessions).toHaveBeenCalledTimes(2);
    expect(tree?.sessionsOf(project.id).sessions).toEqual([session]);
  });

  it('commits concurrent reads for independent project resources', async () => {
    const projectARead = deferred({ sessions: [session], next_before_id: null });
    const projectBRead = deferred({ sessions: [sessionB], next_before_id: null });
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({
        projects: [project, projectB],
        sessions: {},
      }),
      listSessions: vi.fn(({ projectId }) => (
        projectId === project.id ? projectARead.promise : projectBRead.promise
      )),
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
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
    await settle();

    act(() => {
      tree?.toggleExpanded(project.id);
      tree?.toggleExpanded(projectB.id);
    });
    await settle();
    await act(async () => {
      projectBRead.resolve({ sessions: [sessionB], next_before_id: null });
      await projectBRead.promise;
    });
    await act(async () => {
      projectARead.resolve({ sessions: [session], next_before_id: null });
      await projectARead.promise;
    });

    expect(tree?.sessionsOf(project.id).sessions).toEqual([session]);
    expect(tree?.sessionsOf(projectB.id).sessions).toEqual([sessionB]);
  });

  it('requeues a project reconcile invalidated by a lifecycle event', async () => {
    const loadedSessions = Array.from({ length: 201 }, (_, index) => ({ ...session, id: `ses_${index}` }));
    const trackedSession = loadedSessions[0];
    const staleRead = deferred({ sessions: [trackedSession], next_before_id: null });
    const listSessions = vi.fn()
      .mockReturnValueOnce(staleRead.promise)
      .mockResolvedValueOnce({ sessions: [{ ...trackedSession, agent_status: 'idle' }], next_before_id: null });
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({
        projects: [project],
        sessions: { [project.id]: { sessions: loadedSessions, next_before_id: null } },
      }),
      listSessions,
      getSession: vi.fn().mockResolvedValue(trackedSession),
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let handlers: WorkbenchEventHandlers | null = null;
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
    await settle();
    act(() => {
      handlers?.onConnected?.({ sub_id: 2, source: 'browser' });
    });
    await settle();
    act(() => {
      handlers?.onSessionStatus?.({ session_id: trackedSession.id, agent_status: 'idle' });
    });
    await act(async () => {
      staleRead.resolve({ sessions: [trackedSession], next_before_id: null });
      await staleRead.promise;
    });
    await settle();

    expect(listSessions).toHaveBeenCalledTimes(2);
    expect(tree?.sessionsOf(project.id).sessions).toEqual([{ ...trackedSession, agent_status: 'idle' }]);
  });

  it('retries a created-session reconcile invalidated before the row is cached', async () => {
    const staleRead = deferred({ sessions: [], next_before_id: null });
    const listSessions = vi.fn()
      .mockReturnValueOnce(staleRead.promise)
      .mockResolvedValueOnce({ sessions: [session], next_before_id: null });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({
        projects: [project],
        sessions: { [project.id]: { sessions: [], next_before_id: null } },
      }),
      listSessions,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'created',
      });
    });
    await settle();
    act(() => {
      handlers?.onSessionStatus?.({ session_id: session.id, agent_status: 'idle' });
    });
    await act(async () => {
      staleRead.resolve({ sessions: [], next_before_id: null });
      await staleRead.promise;
    });
    await settle();

    expect(listSessions).toHaveBeenCalledTimes(2);
    expect(tree?.sessionsOf(project.id).sessions).toEqual([session]);
  });

  it('coalesces cached-row refreshes and retries after a newer lifecycle mutation', async () => {
    const staleRowRead = deferred({ ...session, native_session_id: 'native_stale' });
    const boundSession = { ...session, native_session_id: 'native_fresh' };
    const getSession = vi.fn()
      .mockReturnValueOnce(staleRowRead.promise)
      .mockResolvedValueOnce(boundSession);
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({
        projects: [project],
        sessions: { [project.id]: { sessions: [session], next_before_id: null } },
      }),
      getSession,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();
    act(() => {
      handlers?.onSessionStatus?.({ session_id: session.id, agent_status: 'idle' });
    });
    await settle();
    act(() => {
      handlers?.onTurnEnd?.({ session_id: session.id });
    });
    await settle();

    expect(getSession).toHaveBeenCalledTimes(1);
    await act(async () => {
      staleRowRead.resolve({ ...session, native_session_id: 'native_stale' });
      await staleRowRead.promise;
    });
    await settle();

    expect(getSession).toHaveBeenCalledTimes(2);
    expect(tree?.sessionsOf(project.id).sessions).toEqual([boundSession]);
  });

  it('retries an invalidated project pagination read', async () => {
    const pageSession = { ...sessionB, scope_id: project.scope_id, project_id: project.id };
    const stalePage = deferred({ sessions: [pageSession], next_before_id: null });
    const listSessions = vi.fn()
      .mockReturnValueOnce(stalePage.promise)
      .mockResolvedValueOnce({ sessions: [pageSession], next_before_id: null });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap: vi.fn().mockResolvedValue({
        projects: [project],
        sessions: { [project.id]: { sessions: [session], next_before_id: 'cursor_a' } },
      }),
      listSessions,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();
    act(() => {
      tree?.loadMore(project.id);
    });
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        title: 'Fresh title',
      });
    });
    await act(async () => {
      stalePage.resolve({ sessions: [pageSession], next_before_id: null });
      await stalePage.promise;
    });
    await settle();

    expect(listSessions).toHaveBeenCalledTimes(2);
    expect(tree?.sessionsOf(project.id).sessions?.map((row) => row.id)).toEqual([session.id, pageSession.id]);
    expect(tree?.sessionsOf(project.id).cursor).toBeNull();
  });

  it('retries a reconnect project tree read invalidated by a live event', async () => {
    const staleTree = deferred({ projects: [project], sessions: { [project.id]: { sessions: [session], next_before_id: null } } });
    const refreshedSession = { ...session, title: 'Fresh after reconnect' };
    const getWorkbenchProjectsBootstrap = vi.fn()
      .mockResolvedValueOnce({ projects: [project], sessions: { [project.id]: { sessions: [session], next_before_id: null } } })
      .mockReturnValueOnce(staleTree.promise)
      .mockResolvedValueOnce({ projects: [project], sessions: { [project.id]: { sessions: [refreshedSession], next_before_id: null } } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      getWorkbenchProjectsBootstrap,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
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
    await settle();
    act(() => {
      handlers?.onConnected?.({ sub_id: 3, source: 'browser' });
    });
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        title: 'Fresh after reconnect',
      });
    });
    await act(async () => {
      staleTree.resolve({ projects: [project], sessions: { [project.id]: { sessions: [session], next_before_id: null } } });
      await staleTree.promise;
    });
    await settle();

    expect(getWorkbenchProjectsBootstrap).toHaveBeenCalledTimes(3);
    expect(tree?.sessionsOf(project.id).sessions).toEqual([refreshedSession]);
  });

  it('does not let an Inbox refresh issued before hide restore the card or unread count', async () => {
    const staleRefresh = deferred({ sessions: [inboxRow], next_cursor: 'cursor_a', unread_by_session: { [session.id]: 3 } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox: vi.fn()
        .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: 'cursor_a', unread_by_session: { [session.id]: 3 } })
        .mockReturnValueOnce(staleRefresh.promise)
        .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_a', unread_by_session: {} }),
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();

    act(() => {
      void inbox?.refresh();
    });
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'background',
      });
    });
    await act(async () => {
      staleRefresh.resolve({ sessions: [inboxRow], next_cursor: 'cursor_a', unread_by_session: { [session.id]: 3 } });
      await staleRefresh.promise;
    });
    await settle();

    expect(apiRef.current?.listInbox).toHaveBeenCalledTimes(3);
    expect(inbox?.inboxSessions).toEqual([]);
    expect(inbox?.unreadBySession).toEqual({});
    expect(inbox?.nextCursor).toBe('cursor_a');
  });

  it('retries a cold Inbox refresh invalidated by activity', async () => {
    const staleRefresh = deferred({ sessions: [inboxRow], next_cursor: 'stale_cursor', unread_by_session: { [session.id]: 3 } });
    const listInbox = vi.fn()
      .mockReturnValueOnce(staleRefresh.promise)
      .mockResolvedValueOnce({ sessions: [], next_cursor: null, unread_by_session: {} });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();

    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'background',
      });
    });
    await act(async () => {
      staleRefresh.resolve({ sessions: [inboxRow], next_cursor: 'stale_cursor', unread_by_session: { [session.id]: 3 } });
      await staleRefresh.promise;
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(2);
    expect(inbox?.inboxSessions).toEqual([]);
    expect(inbox?.unreadBySession).toEqual({});
    expect(inbox?.nextCursor).toBeNull();
  });

  it('does not let an older mark-read response restore unread after hide', async () => {
    const staleMarkRead = deferred({ unread_by_session: { [session.id]: 2 } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox: vi.fn().mockResolvedValue({
        sessions: [inboxRow],
        next_cursor: null,
        unread_by_session: { [session.id]: 3 },
      }),
      markSessionRead: vi.fn().mockReturnValue(staleMarkRead.promise),
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      void inbox?.markRead(session.id);
    });
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'background',
      });
    });
    await act(async () => {
      staleMarkRead.resolve({ unread_by_session: { [session.id]: 2 } });
      await staleMarkRead.promise;
    });
    await settle();

    expect(inbox?.inboxSessions).toEqual([]);
    expect(inbox?.unreadBySession).toEqual({});
  });

  it('does not treat targeted unread data as a complete account snapshot', async () => {
    const staleInitialRead = deferred({
      sessions: [],
      next_cursor: null,
      unread_by_session: { [sessionB.id]: 5 },
    });
    const replacementFailure = new Error('replacement refresh failed');
    const listInbox = vi.fn()
      .mockReturnValueOnce(staleInitialRead.promise)
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: null,
        unread_by_session: { [session.id]: 3 },
      })
      .mockRejectedValueOnce(replacementFailure);
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    const favicon = document.createElement('link');
    favicon.rel = 'icon';
    favicon.href = '/logo.png';
    document.head.appendChild(favicon);
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
    });
    await settle();
    expect(inbox?.unreadBySession).toEqual({ [session.id]: 3 });
    await act(async () => {
      staleInitialRead.resolve({
        sessions: [],
        next_cursor: null,
        unread_by_session: { [sessionB.id]: 5 },
      });
      await staleInitialRead.promise;
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(3);
    expect(inbox?.unreadBySession).toEqual({ [session.id]: 3 });
    expect(favicon.getAttribute('href')).toBe('/logo.png');
    expect(consoleError).toHaveBeenCalledWith('[inbox] refresh failed', replacementFailure);
    favicon.remove();
    consoleError.mockRestore();
  });

  it('keeps a mark-read result across an unrelated unread event', async () => {
    const markReadResult = deferred({ unread_by_session: { [sessionB.id]: 4 } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox: vi.fn().mockResolvedValue({
        sessions: [inboxRow],
        next_cursor: null,
        unread_by_session: { [session.id]: 3, [sessionB.id]: 5 },
      }),
      markSessionRead: vi.fn().mockReturnValue(markReadResult.promise),
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      void inbox?.markRead(session.id);
    });
    await settle();
    act(() => {
      handlers?.onInboxUnreadChanged?.({
        session_id: sessionB.id,
        scope_id: sessionB.scope_id,
        delta: -1,
        unread_counts: { agent: 7 },
        unread_by_session: { [session.id]: 3, [sessionB.id]: 4 },
      });
    });
    await act(async () => {
      markReadResult.resolve({ unread_by_session: { [sessionB.id]: 4 } });
      await markReadResult.promise;
    });
    await settle();

    expect(inbox?.unreadBySession).toEqual({ [sessionB.id]: 4 });
  });

  it('keeps a successful mark-read result when an older unread snapshot finishes last', async () => {
    const markReadResult = deferred({ unread_by_session: { [sessionB.id]: 5 } });
    const staleRefresh = deferred({
      sessions: [inboxRow],
      next_cursor: 'stale_cursor',
      unread_by_session: { [session.id]: 3, [sessionB.id]: 5 },
    });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: 'cursor_a',
        unread_by_session: { [session.id]: 3, [sessionB.id]: 5 },
      })
      .mockReturnValueOnce(staleRefresh.promise);
    apiRef.current = {
      listInbox,
      markSessionRead: vi.fn().mockReturnValue(markReadResult.promise),
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      void inbox?.markRead(session.id);
      void inbox?.refresh();
    });
    await settle();
    await act(async () => {
      markReadResult.resolve({ unread_by_session: { [sessionB.id]: 5 } });
      await markReadResult.promise;
    });
    await act(async () => {
      staleRefresh.resolve({
        sessions: [inboxRow],
        next_cursor: 'stale_cursor',
        unread_by_session: { [session.id]: 3, [sessionB.id]: 5 },
      });
      await staleRefresh.promise;
    });
    await settle();

    expect(inbox?.unreadBySession).toEqual({ [sessionB.id]: 5 });
  });

  it('defers a later load-more until an in-flight Inbox refresh completes', async () => {
    const pageRow = {
      ...inboxRow,
      session_id: sessionB.id,
      scope_id: sessionB.scope_id,
      title: sessionB.title,
      preview_message_id: 'msg_b',
      last_activity_at: '2026-08-10T23:00:00Z',
    };
    const refreshedRow = { ...inboxRow, title: 'Fresh before deferred load-more' };
    const refreshRead = deferred({
      sessions: [refreshedRow],
      next_cursor: 'cursor_after_refresh',
      unread_by_session: { [session.id]: 3 },
    });
    const loadMoreRead = deferred({
      sessions: [pageRow],
      next_cursor: 'cursor_after_load_more',
      unread_by_session: { [session.id]: 3 },
    });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: 'cursor_a',
        unread_by_session: { [session.id]: 3 },
      })
      .mockReturnValueOnce(refreshRead.promise)
      .mockReturnValueOnce(loadMoreRead.promise);
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      void inbox?.refresh();
    });
    await settle();
    act(() => {
      void inbox?.loadMore();
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(2);
    await act(async () => {
      refreshRead.resolve({
        sessions: [refreshedRow],
        next_cursor: 'cursor_after_refresh',
        unread_by_session: { [session.id]: 3 },
      });
      await refreshRead.promise;
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(3);
    expect(listInbox.mock.calls[2]?.[0]).toMatchObject({ before: 'cursor_after_refresh' });
    await act(async () => {
      loadMoreRead.resolve({
        sessions: [pageRow],
        next_cursor: 'cursor_after_load_more',
        unread_by_session: { [session.id]: 3 },
      });
      await loadMoreRead.promise;
    });
    await settle();

    expect(inbox?.inboxSessions).toEqual([refreshedRow, pageRow]);
    expect(inbox?.nextCursor).toBe('cursor_after_load_more');
  });

  it('defers a later Inbox refresh until an in-flight load-more completes', async () => {
    const pageRow = {
      ...inboxRow,
      session_id: sessionB.id,
      scope_id: sessionB.scope_id,
      title: sessionB.title,
      preview_message_id: 'msg_b',
      last_activity_at: '2026-08-10T23:00:00Z',
    };
    const loadMoreRead = deferred({
      sessions: [pageRow],
      next_cursor: 'cursor_after_load_more',
      unread_by_session: { [session.id]: 3 },
    });
    const refreshedRow = { ...inboxRow, title: 'Fresh after deferred refresh' };
    const refreshRead = deferred({
      sessions: [refreshedRow],
      next_cursor: 'cursor_after_refresh',
      unread_by_session: { [session.id]: 3 },
    });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: 'cursor_a',
        unread_by_session: { [session.id]: 3 },
      })
      .mockReturnValueOnce(loadMoreRead.promise)
      .mockReturnValueOnce(refreshRead.promise);
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      void inbox?.loadMore();
      void inbox?.refresh();
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(2);
    await act(async () => {
      loadMoreRead.resolve({
        sessions: [pageRow],
        next_cursor: 'cursor_after_load_more',
        unread_by_session: { [session.id]: 3 },
      });
      await loadMoreRead.promise;
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(3);
    await act(async () => {
      refreshRead.resolve({
        sessions: [refreshedRow],
        next_cursor: 'cursor_after_refresh',
        unread_by_session: { [session.id]: 3 },
      });
      await refreshRead.promise;
    });
    await settle();

    expect(inbox?.inboxSessions).toEqual([refreshedRow]);
    expect(inbox?.nextCursor).toBe('cursor_after_refresh');
  });

  it('retries a resume reconcile invalidated by activity', async () => {
    const staleReconcile = deferred({ sessions: [inboxRow], next_cursor: 'stale_cursor', unread_by_session: { [session.id]: 3 } });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: 'cursor_a', unread_by_session: { [session.id]: 3 } })
      .mockReturnValueOnce(staleReconcile.promise)
      .mockResolvedValue({ sessions: [], next_cursor: null, unread_by_session: {} });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'background',
      });
    });
    await act(async () => {
      staleReconcile.resolve({ sessions: [inboxRow], next_cursor: 'stale_cursor', unread_by_session: { [session.id]: 3 } });
      await staleReconcile.promise;
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(3);
    expect(inbox?.inboxSessions).toEqual([]);
    expect(inbox?.nextCursor).toBeNull();
  });

  it('defers a later load-more until an in-flight resume reconcile completes', async () => {
    const pageRow = {
      ...inboxRow,
      session_id: sessionB.id,
      scope_id: sessionB.scope_id,
      title: sessionB.title,
      preview_message_id: 'msg_b',
      last_activity_at: '2026-08-10T23:00:00Z',
    };
    const reconciledRow = { ...inboxRow, title: 'Fresh before deferred load-more' };
    const reconcileRead = deferred({
      sessions: [reconciledRow],
      next_cursor: 'cursor_after_reconcile',
      unread_by_session: { [session.id]: 3 },
    });
    const loadMoreRead = deferred({
      sessions: [pageRow],
      next_cursor: 'cursor_after_load_more',
      unread_by_session: { [session.id]: 3 },
    });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: 'cursor_a', unread_by_session: { [session.id]: 3 } })
      .mockReturnValueOnce(reconcileRead.promise)
      .mockReturnValueOnce(loadMoreRead.promise);
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    await settle();
    act(() => {
      void inbox?.loadMore();
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(2);
    await act(async () => {
      reconcileRead.resolve({
        sessions: [reconciledRow],
        next_cursor: 'cursor_after_reconcile',
        unread_by_session: { [session.id]: 3 },
      });
      await reconcileRead.promise;
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(3);
    expect(listInbox.mock.calls[2]?.[0]).toMatchObject({ before: 'cursor_a' });
    await act(async () => {
      loadMoreRead.resolve({
        sessions: [pageRow],
        next_cursor: 'cursor_after_load_more',
        unread_by_session: { [session.id]: 3 },
      });
      await loadMoreRead.promise;
    });
    await settle();

    expect(inbox?.inboxSessions).toEqual([reconciledRow, pageRow]);
    expect(inbox?.nextCursor).toBe('cursor_after_load_more');
  });

  it('runs a queued resume retry before a later load-more', async () => {
    const pageRow = {
      ...inboxRow,
      session_id: sessionB.id,
      scope_id: sessionB.scope_id,
      title: sessionB.title,
      preview_message_id: 'msg_b',
      last_activity_at: '2026-08-10T23:00:00Z',
    };
    const staleReconcile = deferred({
      sessions: [inboxRow],
      next_cursor: 'stale_cursor',
      unread_by_session: { [session.id]: 3 },
    });
    const loadMoreRead = deferred({
      sessions: [pageRow],
      next_cursor: 'cursor_after_load_more',
      unread_by_session: {},
    });
    const retryReconcile = deferred({
      sessions: [],
      next_cursor: 'cursor_after_reconcile',
      unread_by_session: {},
    });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: 'cursor_a',
        unread_by_session: { [session.id]: 3 },
      })
      .mockReturnValueOnce(staleReconcile.promise)
      .mockReturnValueOnce(retryReconcile.promise)
      .mockReturnValueOnce(loadMoreRead.promise);
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'background',
      });
      void inbox?.loadMore();
    });
    await settle();
    await act(async () => {
      staleReconcile.resolve({
        sessions: [inboxRow],
        next_cursor: 'stale_cursor',
        unread_by_session: { [session.id]: 3 },
      });
      await staleReconcile.promise;
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(3);
    await act(async () => {
      retryReconcile.resolve({
        sessions: [],
        next_cursor: 'cursor_after_reconcile',
        unread_by_session: {},
      });
      await retryReconcile.promise;
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(4);
    expect(listInbox.mock.calls[3]?.[0]).toMatchObject({ before: 'cursor_after_reconcile' });
    await act(async () => {
      loadMoreRead.resolve({
        sessions: [pageRow],
        next_cursor: 'cursor_after_load_more',
        unread_by_session: {},
      });
      await loadMoreRead.promise;
    });
    await settle();

    expect(inbox?.inboxSessions.map((row) => row.session_id)).toEqual([sessionB.id]);
    expect(inbox?.nextCursor).toBe('cursor_after_load_more');
  });

  it('defers a later resume reconcile until an in-flight load-more completes', async () => {
    const pageRow = {
      ...inboxRow,
      session_id: sessionB.id,
      scope_id: sessionB.scope_id,
      title: sessionB.title,
      preview_message_id: 'msg_b',
      last_activity_at: '2026-08-10T23:00:00Z',
    };
    const loadMoreRead = deferred({
      sessions: [pageRow],
      next_cursor: 'cursor_after_load_more',
      unread_by_session: { [session.id]: 3 },
    });
    const reconcileRead = deferred({
      sessions: [inboxRow],
      next_cursor: 'cursor_after_reconcile',
      unread_by_session: { [session.id]: 3 },
    });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: 'cursor_a',
        unread_by_session: { [session.id]: 3 },
      })
      .mockReturnValueOnce(loadMoreRead.promise)
      .mockReturnValueOnce(reconcileRead.promise);
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      void inbox?.loadMore();
      window.dispatchEvent(new Event('focus'));
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(2);
    await act(async () => {
      loadMoreRead.resolve({
        sessions: [pageRow],
        next_cursor: 'cursor_after_load_more',
        unread_by_session: { [session.id]: 3 },
      });
      await loadMoreRead.promise;
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(3);
    await act(async () => {
      reconcileRead.resolve({
        sessions: [inboxRow],
        next_cursor: 'cursor_after_reconcile',
        unread_by_session: { [session.id]: 3 },
      });
      await reconcileRead.promise;
    });
    await settle();

    expect(inbox?.inboxSessions.map((row) => row.session_id).sort()).toEqual([
      session.id,
      sessionB.id,
    ]);
    expect(inbox?.nextCursor).toBe('cursor_after_load_more');
  });

  it('serializes duplicate resume reconciles so a later failure cannot discard an earlier success', async () => {
    const reconciledRow = { ...inboxRow, title: 'Recovered from the first reconcile' };
    const firstReconcile = deferred({
      sessions: [reconciledRow],
      next_cursor: 'cursor_after_reconcile',
      unread_by_session: { [session.id]: 3 },
    });
    const laterFailure = new Error('later reconcile failed');
    const listInbox = vi.fn()
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: 'cursor_a',
        unread_by_session: { [session.id]: 3 },
      })
      .mockReturnValueOnce(firstReconcile.promise)
      .mockRejectedValueOnce(laterFailure);
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('focus'));
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(2);
    await act(async () => {
      firstReconcile.resolve({
        sessions: [reconciledRow],
        next_cursor: 'cursor_after_reconcile',
        unread_by_session: { [session.id]: 3 },
      });
      await firstReconcile.promise;
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(3);
    expect(inbox?.inboxSessions).toEqual([reconciledRow]);
    expect(inbox?.nextCursor).toBe('cursor_a');
    expect(consoleError).toHaveBeenCalledWith('[inbox] reconcile failed', laterFailure);
    consoleError.mockRestore();
  });

  it('serializes duplicate refreshes so a later failure cannot discard an earlier success', async () => {
    const refreshedRow = { ...inboxRow, title: 'Recovered from the first refresh' };
    const firstRefresh = deferred({
      sessions: [refreshedRow],
      next_cursor: 'cursor_after_refresh',
      unread_by_session: { [session.id]: 3 },
    });
    const laterFailure = new Error('later refresh failed');
    const listInbox = vi.fn()
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: 'cursor_a',
        unread_by_session: { [session.id]: 3 },
      })
      .mockReturnValueOnce(firstRefresh.promise)
      .mockRejectedValueOnce(laterFailure);
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      void inbox?.refresh();
      void inbox?.refresh();
    });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(2);
    await act(async () => {
      firstRefresh.resolve({
        sessions: [refreshedRow],
        next_cursor: 'cursor_after_refresh',
        unread_by_session: { [session.id]: 3 },
      });
      await firstRefresh.promise;
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(3);
    expect(inbox?.inboxSessions).toEqual([refreshedRow]);
    expect(inbox?.nextCursor).toBe('cursor_after_refresh');
    expect(inbox?.loading).toBe(false);
    expect(consoleError).toHaveBeenCalledWith('[inbox] refresh failed', laterFailure);
    consoleError.mockRestore();
  });

  it('starts the load-more indicator only after a blocking refresh settles', async () => {
    const refreshRead = deferred({ sessions: [inboxRow], next_cursor: 'cursor_refresh', unread_by_session: { [session.id]: 3 } });
    const loadMoreRead = deferred({ sessions: [], next_cursor: null, unread_by_session: { [session.id]: 3 } });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: 'cursor_a', unread_by_session: { [session.id]: 3 } })
      .mockReturnValueOnce(refreshRead.promise)
      .mockReturnValueOnce(loadMoreRead.promise);
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      void inbox?.refresh();
      void inbox?.loadMore();
    });
    await settle();
    expect(inbox?.loading).toBe(true);
    expect(inbox?.loadingMore).toBe(false);
    expect(listInbox).toHaveBeenCalledTimes(2);
    await act(async () => {
      refreshRead.resolve({ sessions: [inboxRow], next_cursor: 'cursor_refresh', unread_by_session: { [session.id]: 3 } });
      await refreshRead.promise;
    });
    await settle();
    expect(inbox?.loading).toBe(false);
    expect(inbox?.loadingMore).toBe(true);
    expect(listInbox).toHaveBeenCalledTimes(3);
    await act(async () => {
      loadMoreRead.resolve({ sessions: [], next_cursor: null, unread_by_session: { [session.id]: 3 } });
      await loadMoreRead.promise;
    });
    await settle();
    expect(inbox?.loading).toBe(false);
    expect(inbox?.loadingMore).toBe(false);
  });

  it('keeps the targeted foreground-restore upsert and its cursor untouched', async () => {
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_before_restore', unread_by_session: {} })
      .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();

    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'created',
        restored: true,
      });
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(2);
    expect(inbox?.inboxSessions.map((row) => row.session_id)).toEqual([session.id]);
    expect(inbox?.unreadBySession).toEqual({ [session.id]: 3 });
    expect(inbox?.nextCursor).toBe('cursor_before_restore');
  });

  it('revalidates a targeted snapshot omitted after a missed removal event', async () => {
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_a', unread_by_session: {} })
      .mockResolvedValueOnce({
        sessions: [inboxRow],
        next_cursor: null,
        unread_by_session: { [session.id]: 3 },
      })
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_after_refresh', unread_by_session: {} })
      .mockResolvedValueOnce({ sessions: [], next_cursor: null, unread_by_session: {} });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
    });
    await settle();
    expect(inbox?.inboxSessions).toEqual([inboxRow]);
    act(() => {
      void inbox?.refresh();
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(4);
    expect(inbox?.inboxSessions).toEqual([]);
    expect(inbox?.unreadBySession).toEqual({});
    expect(inbox?.nextCursor).toBe('cursor_after_refresh');
  });

  it('keeps a targeted restore without replacing a newer broad unread map', async () => {
    const targetedRead = deferred({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
    const broadRead = deferred({ sessions: [], next_cursor: 'cursor_after_refresh', unread_by_session: { [sessionB.id]: 5 } });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_before_restore', unread_by_session: {} })
      .mockReturnValueOnce(targetedRead.promise)
      .mockReturnValueOnce(broadRead.promise)
      .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
      void inbox?.refresh();
    });
    await settle();
    await act(async () => {
      broadRead.resolve({ sessions: [], next_cursor: 'cursor_after_refresh', unread_by_session: { [sessionB.id]: 5 } });
      await broadRead.promise;
    });
    await act(async () => {
      targetedRead.resolve({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
      await targetedRead.promise;
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(4);
    expect(inbox?.inboxSessions.map((row) => row.session_id)).toEqual([session.id]);
    expect(inbox?.unreadBySession).toEqual({ [sessionB.id]: 5, [session.id]: 3 });
    expect(inbox?.nextCursor).toBe('cursor_after_refresh');
  });

  it('revalidates an in-flight targeted read omitted by a newer broad refresh', async () => {
    const targetedRead = deferred({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
    const broadRead = deferred({ sessions: [], next_cursor: 'cursor_after_refresh', unread_by_session: {} });
    const exactRevalidation = deferred({ sessions: [], next_cursor: null, unread_by_session: {} });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_before_restore', unread_by_session: {} })
      .mockReturnValueOnce(targetedRead.promise)
      .mockReturnValueOnce(broadRead.promise)
      .mockReturnValueOnce(exactRevalidation.promise);
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
      void inbox?.refresh();
    });
    await settle();
    await act(async () => {
      broadRead.resolve({ sessions: [], next_cursor: 'cursor_after_refresh', unread_by_session: {} });
      await broadRead.promise;
    });
    await act(async () => {
      targetedRead.resolve({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
      await targetedRead.promise;
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(4);
    expect(inbox?.inboxSessions).toEqual([]);
    expect(inbox?.unreadBySession).toEqual({});
    await act(async () => {
      exactRevalidation.resolve({ sessions: [], next_cursor: null, unread_by_session: {} });
      await exactRevalidation.promise;
    });
    await settle();

    expect(inbox?.inboxSessions).toEqual([]);
    expect(inbox?.unreadBySession).toEqual({});
    expect(inbox?.nextCursor).toBe('cursor_after_refresh');
  });

  it('keeps targeted unread data when a newer broad refresh fails', async () => {
    const targetedRead = deferred({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
    const refreshFailure = new Error('refresh failed');
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_before_restore', unread_by_session: {} })
      .mockReturnValueOnce(targetedRead.promise)
      .mockRejectedValueOnce(refreshFailure);
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
      void inbox?.refresh();
    });
    await settle();
    await act(async () => {
      targetedRead.resolve({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
      await targetedRead.promise;
    });
    await settle();

    expect(inbox?.inboxSessions).toEqual([inboxRow]);
    expect(inbox?.unreadBySession).toEqual({ [session.id]: 3 });
    expect(inbox?.nextCursor).toBe('cursor_before_restore');
    expect(consoleError).toHaveBeenCalledWith('[inbox] refresh failed', refreshFailure);
    consoleError.mockRestore();
  });

  it('lets a newer cursor page replace a targeted session snapshot', async () => {
    const pagedRow = {
      ...inboxRow,
      title: 'Fresh from cursor page',
      last_activity_at: '2026-08-11T02:00:00Z',
    };
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_a', unread_by_session: {} })
      .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } })
      .mockResolvedValueOnce({ sessions: [pagedRow], next_cursor: null, unread_by_session: {} });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
    });
    await settle();
    act(() => {
      void inbox?.loadMore();
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(3);
    expect(listInbox.mock.calls[2]?.[0]).toMatchObject({ before: 'cursor_a' });
    expect(inbox?.inboxSessions).toEqual([pagedRow]);
    expect(inbox?.unreadBySession).toEqual({ [session.id]: 3 });
    expect(inbox?.nextCursor).toBeNull();
  });

  it('does not let an older targeted row replace a newer broad row that completed first', async () => {
    const refreshedRow = {
      ...inboxRow,
      title: 'Fresh from broad feed',
      last_activity_at: '2026-08-11T01:00:00Z',
    };
    const targetedRead = deferred({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
    const broadRead = deferred({
      sessions: [refreshedRow],
      next_cursor: 'cursor_after_refresh',
      unread_by_session: { [session.id]: 4 },
    });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_before_restore', unread_by_session: {} })
      .mockReturnValueOnce(targetedRead.promise)
      .mockReturnValueOnce(broadRead.promise);
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
      void inbox?.refresh();
    });
    await settle();
    await act(async () => {
      broadRead.resolve({
        sessions: [refreshedRow],
        next_cursor: 'cursor_after_refresh',
        unread_by_session: { [session.id]: 4 },
      });
      await broadRead.promise;
    });
    await act(async () => {
      targetedRead.resolve({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
      await targetedRead.promise;
    });
    await settle();

    expect(inbox?.inboxSessions).toEqual([refreshedRow]);
    expect(inbox?.unreadBySession).toEqual({ [session.id]: 4 });
    expect(inbox?.nextCursor).toBe('cursor_after_refresh');
  });

  it('lets a later broad feed replace a targeted session snapshot', async () => {
    const refreshedRow = {
      ...inboxRow,
      title: 'Fresh from broad feed',
      last_activity_at: '2026-08-11T01:00:00Z',
    };
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_before_restore', unread_by_session: {} })
      .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } })
      .mockResolvedValueOnce({ sessions: [refreshedRow], next_cursor: 'cursor_after_refresh', unread_by_session: { [session.id]: 4 } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
    });
    await settle();
    act(() => {
      void inbox?.refresh();
    });
    await settle();

    expect(inbox?.inboxSessions).toEqual([refreshedRow]);
    expect(inbox?.unreadBySession).toEqual({ [session.id]: 4 });
    expect(inbox?.nextCursor).toBe('cursor_after_refresh');
  });

  it('keeps a targeted restore when the broad refresh completes last', async () => {
    const targetedRead = deferred({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
    const broadRead = deferred({ sessions: [], next_cursor: 'cursor_after_refresh', unread_by_session: {} });
    const listInbox = vi.fn()
      .mockResolvedValueOnce({ sessions: [], next_cursor: 'cursor_before_restore', unread_by_session: {} })
      .mockReturnValueOnce(targetedRead.promise)
      .mockReturnValueOnce(broadRead.promise)
      .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: null, unread_by_session: {} });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    let inbox: ReturnType<typeof useWorkbenchInbox> | null = null;
    const Probe = () => {
      const value = useWorkbenchInbox();
      useEffect(() => {
        inbox = value;
      }, [value]);
      return null;
    };

    render(
      <WorkbenchInboxProvider>
        <Probe />
      </WorkbenchInboxProvider>,
    );
    await settle();
    act(() => {
      handlers?.onSessionActivity?.({
        session_id: session.id,
        scope_id: session.scope_id,
        event: 'updated',
        visibility: 'foreground',
      });
      void inbox?.refresh();
    });
    await settle();
    await act(async () => {
      targetedRead.resolve({ sessions: [inboxRow], next_cursor: null, unread_by_session: { [session.id]: 3 } });
      await targetedRead.promise;
    });
    await settle();
    await act(async () => {
      broadRead.resolve({ sessions: [], next_cursor: 'cursor_after_refresh', unread_by_session: {} });
      await broadRead.promise;
    });
    await settle();

    expect(listInbox).toHaveBeenCalledTimes(4);
    expect(inbox?.inboxSessions.map((row) => row.session_id)).toEqual([session.id]);
    expect(inbox?.unreadBySession).toEqual({});
    expect(inbox?.nextCursor).toBe('cursor_after_refresh');
  });
});
