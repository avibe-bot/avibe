// @vitest-environment jsdom

import { act, render } from '@testing-library/react';
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
  listInbox?: () => Promise<unknown>;
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

  it('does not let an Inbox refresh issued before hide restore the card or unread count', async () => {
    const staleRefresh = deferred({ sessions: [inboxRow], next_cursor: 'cursor_a', unread_by_session: { [session.id]: 3 } });
    let handlers: WorkbenchEventHandlers | null = null;
    apiRef.current = {
      listInbox: vi.fn()
        .mockResolvedValueOnce({ sessions: [inboxRow], next_cursor: 'cursor_a', unread_by_session: { [session.id]: 3 } })
        .mockReturnValueOnce(staleRefresh.promise),
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
});
