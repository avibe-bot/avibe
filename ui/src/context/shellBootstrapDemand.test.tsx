// @vitest-environment jsdom

// One class: a provider mounted ABOVE the router bootstraps on its first real
// consumer, not on mount. The providers below are shared by every route, so a
// mount-time read makes every route pay for data it may never render — the
// project tree on /admin, thirty rows of inbox feed on a page that shows only a
// badge. These tests state the property (what the document actually reads
// decides what is fetched) rather than enumerating which routes are exempt, so a
// route added later inherits the rule.

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkbenchInboxProvider } from './WorkbenchInboxProvider';
import { useWorkbenchInbox, type InboxState } from './WorkbenchInboxContext';
import { WorkbenchProjectsProvider } from './WorkbenchProjectsProvider';
import { useWorkbenchProjectsTree, type WorkbenchProjectsTree } from './WorkbenchProjectsContext';
import type { WorkbenchEventHandlers } from './ApiContext';

const project = {
  id: 'proj_a',
  scope_id: 'scope_a',
  display_name: 'Project A',
  folder_path: '/tmp/project-a',
  created_at: '2026-08-18T00:00:00Z',
  last_active_at: null,
  archived: false,
};

const session = {
  id: 'ses_a',
  scope_id: 'scope_a',
  project_id: 'proj_a',
  title: 'A session',
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
  created_at: '2026-08-18T00:00:00Z',
  updated_at: '2026-08-18T00:00:00Z',
  last_active_at: '2026-08-18T00:00:00Z',
  metadata: {},
};

const bootstrapPayload = {
  projects: [project],
  sessions: { proj_a: { sessions: [session], next_before_id: null } },
};

const inboxRow = {
  session_id: session.id,
  scope_id: session.scope_id,
  title: session.title,
  preview: 'A feed row',
  preview_message_id: 'msg_a',
  last_activity_at: session.last_active_at,
  unread_count: 3,
  visibility: 'foreground' as const,
};

// Every read of /api/inbox answers with a feed row AND a cursor alongside the
// unread map, exactly as the server does. A counts-only read must discard both.
const inboxPayload = {
  sessions: [inboxRow],
  next_cursor: 'cursor_1',
  unread_by_session: { [session.id]: 3 },
};

type ListInboxArgs = { platform: string; limit: number; before?: string };

type FakeApi = {
  getWorkbenchProjectsBootstrap?: () => Promise<unknown>;
  listSessions?: () => Promise<unknown>;
  getSession?: () => Promise<unknown>;
  listInbox?: (args: ListInboxArgs) => Promise<unknown>;
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const TreeProbe = ({ onState }: { onState?: (tree: WorkbenchProjectsTree) => void } = {}) => {
  const tree = useWorkbenchProjectsTree();
  useEffect(() => {
    onState?.(tree);
  }, [onState, tree]);
  return null;
};

const InboxProbe = ({
  feed,
  onState,
}: {
  feed: boolean;
  onState?: (state: InboxState) => void;
}) => {
  const state = useWorkbenchInbox({ feed });
  useEffect(() => {
    onState?.(state);
  }, [onState, state]);
  return null;
};

describe('Demand-driven shell bootstrap', () => {
  beforeEach(() => {
    apiRef.current = null;
  });

  afterEach(() => {
    cleanup();
    apiRef.current = null;
  });

  describe('projects tree', () => {
    it('stays unfetched while the document renders no project', async () => {
      const bootstrap = vi.fn().mockResolvedValue(bootstrapPayload);
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        connectWorkbenchEvents: vi.fn(() => vi.fn()),
      };

      render(
        <WorkbenchProjectsProvider>
          <div />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      expect(bootstrap).not.toHaveBeenCalled();
    });

    it('bootstraps once for a document that reads the tree', async () => {
      const bootstrap = vi.fn().mockResolvedValue(bootstrapPayload);
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        connectWorkbenchEvents: vi.fn(() => vi.fn()),
      };

      render(
        <WorkbenchProjectsProvider>
          <TreeProbe />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      expect(bootstrap).toHaveBeenCalledTimes(1);
    });

    it('does not re-bootstrap on a reconnect while nothing reads the tree', async () => {
      const bootstrap = vi.fn().mockResolvedValue(bootstrapPayload);
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };

      render(
        <WorkbenchProjectsProvider>
          <div />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      // A long-lived tab sees the shared stream flap and re-authorize; neither is
      // a reason to load a tree the document does not render.
      await act(async () => {
        handlers?.onConnected?.();
        handlers?.onAuthorizationChanged?.();
      });
      await settle();

      expect(bootstrap).not.toHaveBeenCalled();
    });
  });

  describe('inbox', () => {
    it('reads only the unread map for a document that renders no feed', async () => {
      const listInbox = vi.fn().mockResolvedValue(inboxPayload);
      apiRef.current = { listInbox, connectWorkbenchEvents: vi.fn(() => vi.fn()) };
      let state: InboxState | null = null;

      render(
        <WorkbenchInboxProvider>
          <InboxProbe
            feed={false}
            onState={(next) => {
              state = next;
            }}
          />
        </WorkbenchInboxProvider>,
      );
      await settle();

      expect(listInbox).toHaveBeenCalledTimes(1);
      expect(listInbox.mock.calls[0][0]).toMatchObject({ platform: 'avibe', limit: 1 });
      // The badge is live; the feed and its cursor are untouched, so activating
      // the feed later still starts from a real first page.
      expect(state?.totalUnread).toBe(3);
      expect(state?.unreadBySession).toEqual({ [session.id]: 3 });
      expect(state?.inboxSessions).toEqual([]);
      expect(state?.nextCursor).toBeNull();
    });

    it('reads the feed page instead of the counts when the document renders a feed', async () => {
      const listInbox = vi.fn().mockResolvedValue(inboxPayload);
      apiRef.current = { listInbox, connectWorkbenchEvents: vi.fn(() => vi.fn()) };
      let state: InboxState | null = null;

      render(
        <WorkbenchInboxProvider>
          <InboxProbe
            feed
            onState={(next) => {
              state = next;
            }}
          />
        </WorkbenchInboxProvider>,
      );
      await settle();

      // One read, not two: a feed consumer activates from its own effect, which
      // React runs before the provider's, so the counts-only read is skipped in
      // favour of the feed read that supersedes it.
      expect(listInbox).toHaveBeenCalledTimes(1);
      expect(listInbox.mock.calls[0][0]).toMatchObject({ platform: 'avibe', limit: 30 });
      expect(state?.inboxSessions).toHaveLength(1);
      expect(state?.nextCursor).toBe('cursor_1');
    });

    it('loads page one when a later navigation brings a feed consumer in', async () => {
      const listInbox = vi.fn().mockResolvedValue(inboxPayload);
      apiRef.current = { listInbox, connectWorkbenchEvents: vi.fn(() => vi.fn()) };
      let state: InboxState | null = null;
      const capture = (next: InboxState) => {
        state = next;
      };

      const { rerender } = render(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();
      expect(listInbox).toHaveBeenCalledTimes(1);

      rerender(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
          <InboxProbe feed />
        </WorkbenchInboxProvider>,
      );
      await settle();

      expect(listInbox).toHaveBeenCalledTimes(2);
      expect(listInbox.mock.calls[1][0]).toMatchObject({ platform: 'avibe', limit: 30 });
      expect(listInbox.mock.calls[1][0]).not.toHaveProperty('before');
      expect(state?.inboxSessions).toHaveLength(1);
    });
  });

  // The demand gate decides what is READ. It must not also decide what stays
  // cached: a reconnect says "you may have missed events" and can wait for a
  // consumer, but an authorization change says "what you hold may no longer be
  // authorized", and both providers' resumption paths deliberately preserve rows
  // (the projects list survives a failed retry; the inbox reconcile keeps rows the
  // response omitted). These pin the drop WITHOUT letting any response resolve, so
  // they hold for a revalidation that is slow, failing, or never arrives.
  describe('authorization changes invalidate what nothing is reading', () => {
    it('drops the cached project tree rather than keeping it for the way back', async () => {
      const bootstrap = vi.fn().mockResolvedValue(bootstrapPayload);
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };
      let tree: WorkbenchProjectsTree | null = null;
      const capture = (next: WorkbenchProjectsTree) => {
        tree = next;
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe onState={capture} />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(tree?.projects).toHaveLength(1);

      // Navigate to a route that renders no project, then lose access there.
      rerender(
        <WorkbenchProjectsProvider>
          <div />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      bootstrap.mockReturnValue(deferred<typeof bootstrapPayload>().promise);
      await act(async () => {
        handlers?.onAuthorizationChanged?.();
      });

      // Nothing re-reads the tree here, and nothing needs to: coming back must not
      // be able to render a project that was authorized before the change.
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe onState={capture} />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(tree?.projects).toBeNull();
      expect(tree?.sessionsOf(project.id).sessions).toBeNull();
      expect(tree?.isExpanded(project.id)).toBe(false);
    });

    it('drops cached inbox rows and makes the next feed read authoritative', async () => {
      const listInbox = vi.fn().mockResolvedValue(inboxPayload);
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        listInbox,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };
      let state: InboxState | null = null;
      const capture = (next: InboxState) => {
        state = next;
      };

      const { rerender } = render(
        <WorkbenchInboxProvider>
          <InboxProbe feed onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();
      expect(state?.inboxSessions).toHaveLength(1);

      rerender(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();
      listInbox.mockReturnValue(deferred<typeof inboxPayload>().promise);
      await act(async () => {
        handlers?.onAuthorizationChanged?.();
      });

      expect(state?.inboxSessions).toEqual([]);
      expect(state?.nextCursor).toBeNull();

      // Returning to a feed must reload page one destructively, not reconcile onto
      // a window whose rows were just invalidated.
      listInbox.mockResolvedValue({ sessions: [], next_cursor: null, unread_by_session: {} });
      rerender(
        <WorkbenchInboxProvider>
          <InboxProbe feed onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();

      const feedRead = listInbox.mock.calls.at(-1)?.[0];
      expect(feedRead).toMatchObject({ platform: 'avibe', limit: 30 });
      expect(feedRead).not.toHaveProperty('before');
      // reconcile() re-reads with cache disabled; page one does not.
      expect(feedRead).not.toHaveProperty('cache');
      expect(state?.inboxSessions).toEqual([]);
    });

    it('re-reads the unread map when a mutation invalidates the counts read', async () => {
      const first = deferred<typeof inboxPayload>();
      const second = deferred<typeof inboxPayload>();
      const listInbox = vi
        .fn()
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise);
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        listInbox,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };
      let state: InboxState | null = null;

      render(
        <WorkbenchInboxProvider>
          <InboxProbe
            feed={false}
            onState={(next) => {
              state = next;
            }}
          />
        </WorkbenchInboxProvider>,
      );
      await settle();
      expect(listInbox).toHaveBeenCalledTimes(1);

      // A live event lands while the counts read is in flight, so that response is
      // no longer authoritative — and the event itself only carries one session's
      // count. Discarding it silently would leave every other session's badge
      // missing until some later focus or reconnect.
      await act(async () => {
        handlers?.onInboxSessionUpdated?.(inboxRow);
      });
      await act(async () => {
        first.resolve({ ...inboxPayload, unread_by_session: { [session.id]: 3, ses_b: 5 } });
      });
      await settle();

      expect(listInbox).toHaveBeenCalledTimes(2);
      expect(listInbox.mock.calls[1][0]).toMatchObject({ platform: 'avibe', limit: 1 });

      await act(async () => {
        second.resolve({ ...inboxPayload, sessions: [], unread_by_session: { ses_b: 7 } });
      });
      await settle();

      expect(state?.unreadBySession).toEqual({ ses_b: 7 });
      expect(state?.totalUnread).toBe(7);
    });
  });
});
