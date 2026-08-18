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
import {
  useWorkbenchProjectsActions,
  useWorkbenchProjectsTree,
  type WorkbenchProjectsActions,
  type WorkbenchProjectsTree,
} from './WorkbenchProjectsContext';
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

type ListInboxArgs = { platform: string; limit: number; before?: string; onlySession?: string };

type FakeApi = {
  getWorkbenchProjectsBootstrap?: () => Promise<unknown>;
  listSessions?: () => Promise<unknown>;
  getSession?: () => Promise<unknown>;
  updateSession?: () => Promise<unknown>;
  createSession?: () => Promise<unknown>;
  forkSession?: (sessionId: string) => Promise<unknown>;
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

// `active={false}` is the real opt-out a tree reader has (a surface that renders
// from provider state without being the reason it loads). Here it doubles as an
// observer that can watch provider state across a navigation that removes every
// activating consumer.
const TreeProbe = ({
  active = true,
  onState,
}: { active?: boolean; onState?: (tree: WorkbenchProjectsTree) => void } = {}) => {
  const tree = useWorkbenchProjectsTree({ active });
  useEffect(() => {
    onState?.(tree);
  }, [onState, tree]);
  return null;
};

// A write-only surface (new-session dialogs, rename, archive) reaches the
// provider through the mutation half of the contract, which cannot read
// `projects` and must not make the document load them.
const ActionsProbe = ({ onState }: { onState?: (actions: WorkbenchProjectsActions) => void }) => {
  const actions = useWorkbenchProjectsActions();
  useEffect(() => {
    onState?.(actions);
  }, [actions, onState]);
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

    // Demand is a property of what a consumer ASKS FOR, not of which route it sits
    // on: a permanently mounted write-only surface (fork, pin, rename, archive)
    // would otherwise re-eagerize the bootstrap on every route through the back
    // door. The mutation half of the contract cannot read `projects`, so it has
    // nothing to load.
    it('leaves the tree unfetched for a document whose only consumer mutates it', async () => {
      const bootstrap = vi.fn().mockResolvedValue(bootstrapPayload);
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        connectWorkbenchEvents: vi.fn(() => vi.fn()),
      };
      let actions: WorkbenchProjectsActions | null = null;
      const capture = (next: WorkbenchProjectsActions) => {
        actions = next;
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <ActionsProbe onState={capture} />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      expect(bootstrap).not.toHaveBeenCalled();
      // The writes are live regardless — they patch whatever is cached.
      expect(actions?.createSessionForProject).toBeTypeOf('function');

      // Non-activating is not inert: the same document loads the tree the moment
      // something actually renders it.
      rerender(
        <WorkbenchProjectsProvider>
          <ActionsProbe onState={capture} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      expect(bootstrap).toHaveBeenCalledTimes(1);
    });

    // Activation has two cases, and they are not interchangeable: the first load
    // builds the tree, a return REVALIDATES whatever the tree already holds. A
    // consumer that paged a project in has a window wider than page one, so
    // resuming through the first-load path would silently truncate it — the same
    // defect the reconnect reconcile exists to prevent, arriving through
    // navigation instead of through the stream.
    it('revalidates a paged-in window when a consumer returns, instead of truncating it', async () => {
      const secondSession = { ...session, id: 'ses_b', title: 'Older session' };
      const bootstrap = vi.fn(async (args?: { projectIds?: string[] }) => ({
        projects: [project],
        sessions: {
          proj_a: args?.projectIds
            ? { sessions: [session, secondSession], next_before_id: null }
            : { sessions: [session], next_before_id: session.id },
        },
      }));
      const listSessions = vi
        .fn()
        .mockResolvedValue({ sessions: [secondSession], next_before_id: null });
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        listSessions,
        connectWorkbenchEvents: vi.fn(() => vi.fn()),
      };
      let tree: WorkbenchProjectsTree | null = null;
      const capture = (next: WorkbenchProjectsTree) => {
        tree = next;
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={capture} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(tree?.sessionsOf(project.id).sessions).toHaveLength(1);

      await act(async () => {
        tree?.loadMore(project.id);
      });
      await settle();
      expect(tree?.sessionsOf(project.id).sessions).toHaveLength(2);

      // Navigate away from every surface that renders the tree, then back.
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={capture} />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={capture} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      expect(bootstrap).toHaveBeenCalledTimes(2);
      const returningRead = bootstrap.mock.calls.at(-1)?.[0] as
        | { projectIds?: string[]; limit?: number; cache?: boolean }
        | undefined;
      expect(returningRead).toMatchObject({ projectIds: [project.id], cache: false });
      expect(returningRead?.limit).toBeGreaterThanOrEqual(2);
      expect(tree?.sessionsOf(project.id).sessions).toHaveLength(2);
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

    // Invalidating a read that is already in flight is itself what queues its
    // replacement, so the drop above has a second half: that retry must not
    // survive the last reader. Otherwise the invalidation repopulates exactly what
    // it just dropped — off-route, and with a snapshot the server produced BEFORE
    // the authorization change.
    it('drops the tree retry the invalidation itself queues', async () => {
      const stale = deferred<typeof bootstrapPayload>();
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
          <TreeProbe active={false} onState={capture} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(tree?.projects).toHaveLength(1);

      // A reconnect reconcile is in flight when the user navigates away.
      bootstrap.mockReturnValue(stale.promise);
      await act(async () => {
        handlers?.onConnected?.();
      });
      expect(bootstrap).toHaveBeenCalledTimes(2);

      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={capture} />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      await act(async () => {
        handlers?.onAuthorizationChanged?.();
      });
      expect(tree?.projects).toBeNull();

      // The pre-change response now arrives and is correctly refused. What it
      // queues in its place has no reader, so it is dropped rather than issued.
      await act(async () => {
        stale.resolve(bootstrapPayload);
      });
      await settle();

      expect(bootstrap).toHaveBeenCalledTimes(2);
      expect(tree?.projects).toBeNull();
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

    it('drops the feed retry the invalidation itself queues', async () => {
      const stale = deferred<typeof inboxPayload>();
      const countsOnly = { sessions: [], next_cursor: null, unread_by_session: { [session.id]: 3 } };
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
      const feedReads = () => listInbox.mock.calls.filter(([args]) => args.limit > 1).length;

      const { rerender } = render(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
          <InboxProbe feed />
        </WorkbenchInboxProvider>,
      );
      await settle();
      expect(state?.inboxSessions).toHaveLength(1);
      expect(feedReads()).toBe(1);

      // A resume reconcile is in flight when the user navigates away. The
      // counts-only read stays live throughout — it is what the remaining route
      // legitimately reads.
      listInbox.mockImplementation((args: ListInboxArgs) =>
        args.limit > 1 ? stale.promise : Promise.resolve(countsOnly),
      );
      await act(async () => {
        window.dispatchEvent(new Event('focus'));
      });
      expect(feedReads()).toBe(2);

      rerender(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();
      await act(async () => {
        handlers?.onAuthorizationChanged?.();
      });
      expect(state?.inboxSessions).toEqual([]);

      await act(async () => {
        stale.resolve(inboxPayload);
      });
      await settle();

      expect(feedReads()).toBe(2);
      expect(state?.inboxSessions).toEqual([]);
      expect(state?.nextCursor).toBeNull();
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

  // The one thing in this provider that NO route is exempt from: every document
  // badges the unread total on its favicon and app icon. So the demand gate may
  // decide whether the 30-row feed is read, but never whether the whole-account
  // map is — and the feed read is the one that usually carries it. These state that
  // as a debt rather than as a list of reads: whichever read was supposed to
  // deliver the map may fail, be invalidated, or be dropped by the gate, and the
  // badge still ends up with one.
  describe('the unread map no route gates', () => {
    it('reads the counts a dropped feed read was carrying', async () => {
      const feedRead = deferred<typeof inboxPayload>();
      const countsOnly = {
        sessions: [inboxRow],
        next_cursor: 'cursor_1',
        unread_by_session: { [session.id]: 3, ses_b: 5 },
      };
      const listInbox = vi.fn((args: ListInboxArgs) =>
        args.limit > 1 ? feedRead.promise : Promise.resolve(countsOnly),
      );
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
      const feedReads = () => listInbox.mock.calls.filter(([args]) => args.limit > 1).length;
      const countsReads = () => listInbox.mock.calls.filter(([args]) => args.limit === 1).length;

      const { rerender } = render(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
          <InboxProbe feed />
        </WorkbenchInboxProvider>,
      );
      await settle();
      expect(feedReads()).toBe(1);
      expect(countsReads()).toBe(0);

      // A live event invalidates that first page while it is still in flight, and
      // carries only its own session's count — so no whole-account map has arrived.
      await act(async () => {
        handlers?.onInboxSessionUpdated?.(inboxRow);
      });
      // The user reaches a feedless route before it settles, so the replacement it
      // queues is dropped: correct for the rows, and the map is now owed to nobody.
      rerender(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();
      await act(async () => {
        feedRead.resolve(inboxPayload);
      });
      await settle();

      // No 30-row read is re-issued for a document that renders no feed...
      expect(feedReads()).toBe(1);
      // ...and the badge does not wait for a later focus, reconnect or permission
      // change to learn every other session's count.
      expect(countsReads()).toBe(1);
      expect(state?.unreadBySession).toEqual({ [session.id]: 3, ses_b: 5 });
      expect(state?.totalUnread).toBe(8);
    });

    it('leaves the counts alone when a healthy feed consumer detaches', async () => {
      const listInbox = vi.fn().mockResolvedValue(inboxPayload);
      apiRef.current = { listInbox, connectWorkbenchEvents: vi.fn(() => vi.fn()) };
      let state: InboxState | null = null;
      const capture = (next: InboxState) => {
        state = next;
      };

      const { rerender } = render(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
          <InboxProbe feed />
        </WorkbenchInboxProvider>,
      );
      await settle();
      expect(listInbox).toHaveBeenCalledTimes(1);

      // Workbench → admin. The feed read already delivered a whole map, so the
      // demand edge owes nothing: re-reading here would put back exactly the
      // per-navigation traffic this provider exists to avoid.
      rerender(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();

      expect(listInbox).toHaveBeenCalledTimes(1);
      expect(state?.unreadBySession).toEqual({ [session.id]: 3 });
    });

    it('reads the counts an authorization change left owing', async () => {
      const stale = deferred<typeof inboxPayload>();
      const postChange = { sessions: [], next_cursor: null, unread_by_session: { ses_b: 5 } };
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
      const feedReads = () => listInbox.mock.calls.filter(([args]) => args.limit > 1).length;

      const { rerender } = render(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
          <InboxProbe feed />
        </WorkbenchInboxProvider>,
      );
      await settle();
      // A whole map HAS arrived, so "have we ever loaded one" is already satisfied
      // here — and is the wrong question, because the map it describes is about to
      // stop describing this account.
      expect(state?.unreadBySession).toEqual({ [session.id]: 3 });

      listInbox.mockImplementation((args: ListInboxArgs) =>
        args.limit > 1 ? stale.promise : Promise.resolve(postChange),
      );
      await act(async () => {
        handlers?.onAuthorizationChanged?.();
      });
      expect(feedReads()).toBe(2);
      // That refresh is invalidated mid-flight, and its replacement is dropped when
      // the user leaves the feed behind — so the read that owed the new scope's map
      // never delivers one.
      await act(async () => {
        handlers?.onInboxSessionUpdated?.(inboxRow);
      });
      rerender(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();
      await act(async () => {
        stale.resolve(inboxPayload);
      });
      await settle();

      expect(feedReads()).toBe(2);
      expect(state?.unreadBySession).toEqual({ ses_b: 5 });
      expect(state?.totalUnread).toBe(5);
    });
  });

  // The gate is a property of the READ, not of the events that trigger it. Written
  // at call sites it has to be remembered by each of them, and these providers have
  // more triggers than any list of guards would cover — a reconnect, session
  // activity, a pin re-order, a status or turn-end row refresh, a foreground
  // restore. So these seed one of every request-issuing shape and assert the
  // property (a revalidation the next activation would redo costs nothing while
  // nothing reads it) rather than enumerating the triggers that were remembered.
  describe('the demand gate belongs to the reads, not to their triggers', () => {
    it('drops every revalidation a stream event or a write triggers while nothing reads the tree', async () => {
      const bootstrap = vi.fn().mockResolvedValue(bootstrapPayload);
      const listSessions = vi.fn().mockResolvedValue({ sessions: [session], next_before_id: null });
      const getSession = vi.fn().mockResolvedValue(session);
      const updateSession = vi.fn().mockResolvedValue({ ...session, pinned: true });
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        listSessions,
        getSession,
        updateSession,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };
      let tree: WorkbenchProjectsTree | null = null;
      let actions: WorkbenchProjectsActions | null = null;
      const captureTree = (next: WorkbenchProjectsTree) => {
        tree = next;
      };
      const captureActions = (next: WorkbenchProjectsActions) => {
        actions = next;
      };
      const reads = () =>
        bootstrap.mock.calls.length + listSessions.mock.calls.length + getSession.mock.calls.length;

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <ActionsProbe onState={captureActions} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(bootstrap).toHaveBeenCalledTimes(1);
      expect(tree?.sessionsOf(project.id).sessions).toHaveLength(1);

      // Workbench → /admin/settings/messaging. The write-only surface stays mounted
      // (a settings page can still rename or pin), so the provider keeps its cache
      // and its stream — it simply has no reader left.
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <ActionsProbe onState={captureActions} />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      const before = reads();

      await act(async () => {
        handlers?.onConnected?.({ sub_id: 1 });
        handlers?.onSessionActivity?.({
          session_id: session.id,
          scope_id: session.scope_id,
          event: 'created',
          restored: true,
        });
        handlers?.onSessionActivity?.({
          session_id: session.id,
          scope_id: session.scope_id,
          event: 'updated',
          pinned: true,
        });
        handlers?.onSessionStatus?.({ session_id: session.id, agent_status: 'idle' });
        handlers?.onTurnEnd?.({ session_id: session.id });
        await actions?.setSessionPinned(project.id, session.id, true);
      });
      await settle();

      // The gate drops revalidations, never mutations: the write still reaches the
      // server and still patches the cache the way back will render.
      expect(updateSession).toHaveBeenCalledTimes(1);
      expect(tree?.sessionsOf(project.id).sessions?.[0]?.pinned).toBe(true);
      expect(reads()).toBe(before);

      // And nothing is lost by dropping them, which is what makes it a revalidation:
      // the returning consumer re-reads the window once.
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <ActionsProbe onState={captureActions} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(bootstrap).toHaveBeenCalledTimes(2);
    });

    // The boundary that scopes the rule: a revalidation is a read the next activation
    // would redo, so dropping it costs nothing. A POPULATING read produces something
    // nothing else will produce — gating it would leave the group the write just
    // expanded rendering empty on the way back, because the reconcile skips projects
    // whose window was never loaded. So the exemption is a property of the read too,
    // not a call site that forgot the gate.
    it('still populates a window a write just created, with nothing reading the tree', async () => {
      const projectB = { ...project, id: 'proj_b', scope_id: 'scope_b', display_name: 'Project B' };
      const createdSession = { ...session, id: 'ses_new', project_id: projectB.id, scope_id: projectB.scope_id };
      const bootstrap = vi.fn().mockResolvedValue({
        projects: [project, projectB],
        sessions: { proj_a: { sessions: [session], next_before_id: null } },
      });
      const listSessions = vi
        .fn()
        .mockResolvedValue({ sessions: [createdSession], next_before_id: null });
      const createSession = vi.fn().mockResolvedValue(createdSession);
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        listSessions,
        createSession,
        connectWorkbenchEvents: vi.fn(() => vi.fn()),
      };
      let tree: WorkbenchProjectsTree | null = null;
      let actions: WorkbenchProjectsActions | null = null;
      const captureTree = (next: WorkbenchProjectsTree) => {
        tree = next;
      };
      const captureActions = (next: WorkbenchProjectsActions) => {
        actions = next;
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <ActionsProbe onState={captureActions} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(tree?.sessionsOf(projectB.id).sessions).toBeNull();

      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <ActionsProbe onState={captureActions} />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      await act(async () => {
        await actions?.createSessionForProject(projectB.id);
      });
      await settle();

      expect(listSessions).toHaveBeenCalledTimes(1);
      expect(listSessions.mock.calls[0][0]).toMatchObject({ projectId: projectB.id });
      expect(tree?.sessionsOf(projectB.id).sessions).toEqual([createdSession]);
    });

    // Exempting the populating reads at the read leaves exactly one demand
    // decision the reads cannot make for themselves: a populating read whose
    // response was refused re-queues ITSELF, and that retry is a revalidation
    // wearing the populating read's name. So the intent flush keeps the gate for
    // that one case and no other — the queued tree reconcile below it is gated by
    // the read it runs, and double-gating it would give one property two owners.
    it('drops the first load a mutation refused once its reader is gone', async () => {
      const firstLoad = deferred<typeof bootstrapPayload>();
      const bootstrap = vi.fn().mockReturnValue(firstLoad.promise);
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };
      let tree: WorkbenchProjectsTree | null = null;
      const captureTree = (next: WorkbenchProjectsTree) => {
        tree = next;
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(bootstrap).toHaveBeenCalledTimes(1);

      // A turn ends while the first load is still in flight, which invalidates it.
      await act(async () => {
        handlers?.onTurnEnd?.({ session_id: session.id });
      });
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      await act(async () => {
        firstLoad.resolve(bootstrapPayload);
      });
      await settle();
      expect(bootstrap).toHaveBeenCalledTimes(1);

      // Dropping it lost nothing: a returning consumer re-reads unconditionally.
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(bootstrap).toHaveBeenCalledTimes(2);
      expect(tree?.projects).toHaveLength(1);
    });

    // The inbox's targeted restore read is the one revalidation that is only HALF
    // droppable: the card it upserts is not rendered, but the same response owns that
    // session's unread count, and `unreadBySession` holds only foreground sessions —
    // so a restore genuinely moves the badge total. Declining the request therefore
    // has to hand the obligation to the whole-account map instead of dropping it.
    it('owes the whole map for a foreground restore instead of reading one session', async () => {
      const beforeRestore = { sessions: [], next_cursor: null, unread_by_session: {} };
      const afterRestore = {
        sessions: [],
        next_cursor: null,
        unread_by_session: { [session.id]: 3, ses_b: 5 },
      };
      const listInbox = vi.fn().mockResolvedValueOnce(beforeRestore).mockResolvedValue(afterRestore);
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
      const wholeCountsReads = () =>
        listInbox.mock.calls.filter(([args]) => args.limit === 1 && !args.onlySession).length;
      const targetedReads = () => listInbox.mock.calls.filter(([args]) => args.onlySession).length;

      render(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();
      expect(wholeCountsReads()).toBe(1);
      expect(state?.totalUnread).toBe(0);

      await act(async () => {
        handlers?.onSessionActivity?.({
          session_id: session.id,
          scope_id: session.scope_id,
          event: 'updated',
          visibility: 'foreground',
        });
      });
      await settle();

      // No card work for a document that renders no card...
      expect(targetedReads()).toBe(0);
      // ...and the badge still learns the count the restore brought back, which a
      // plain skip would have left out until some later focus or reconnect.
      expect(wholeCountsReads()).toBe(2);
      expect(state?.unreadBySession).toEqual({ [session.id]: 3, ses_b: 5 });
      expect(state?.totalUnread).toBe(8);
    });

    it('coalesces a burst of restores into one counts read', async () => {
      const listInbox = vi.fn().mockResolvedValue({
        sessions: [],
        next_cursor: null,
        unread_by_session: { [session.id]: 1, ses_b: 2, ses_c: 3 },
      });
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

      render(
        <WorkbenchInboxProvider>
          <InboxProbe feed={false} onState={capture} />
        </WorkbenchInboxProvider>,
      );
      await settle();
      expect(listInbox).toHaveBeenCalledTimes(1);

      // Restoring a scope restores its sessions together. Owing one map is what
      // makes that cost one read rather than one read per session — the reason the
      // obligation is state and not a per-event request.
      await act(async () => {
        for (const sessionId of [session.id, 'ses_b', 'ses_c']) {
          handlers?.onSessionActivity?.({
            session_id: sessionId,
            scope_id: session.scope_id,
            event: 'updated',
            visibility: 'foreground',
          });
        }
      });
      await settle();

      expect(listInbox).toHaveBeenCalledTimes(2);
      expect(listInbox.mock.calls[1][0]).toMatchObject({ platform: 'avibe', limit: 1 });
      expect(state?.totalUnread).toBe(6);
    });

    // Owing the map and fencing the reads that could pay it are one act. A read that
    // left the server before the change cannot describe the counts it is about to
    // commit, and committing it would pay the debt without satisfying it — the case a
    // bare `setWholeUnreadOwed(true)` cannot even schedule work for, because the debt
    // was already outstanding. Stated over every edge that voids the current map
    // rather than for one of them, because the property belongs to owing the map.
    const mapVoidingEdges: Array<[string, (handlers: WorkbenchEventHandlers | null) => void]> = [
      ['an authorization change', (handlers) => handlers?.onAuthorizationChanged?.({})],
      ['unread counts moving without a map', (handlers) => handlers?.onInboxUnreadChanged?.({ unread_counts: {} })],
    ];
    for (const [edge, fire] of mapVoidingEdges) {
      it(`invalidates the counts read ${edge} left unable to describe the account`, async () => {
        const preChange = deferred<typeof inboxPayload>();
        const listInbox = vi
          .fn()
          .mockReturnValueOnce(preChange.promise)
          .mockResolvedValue({ sessions: [], next_cursor: null, unread_by_session: { ses_b: 5 } });
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

        render(
          <WorkbenchInboxProvider>
            <InboxProbe feed={false} onState={capture} />
          </WorkbenchInboxProvider>,
        );
        await settle();
        expect(listInbox).toHaveBeenCalledTimes(1);

        await act(async () => {
          fire(handlers);
        });
        await act(async () => {
          preChange.resolve({ ...inboxPayload, unread_by_session: { [session.id]: 3 } });
        });
        await settle();

        expect(listInbox).toHaveBeenCalledTimes(2);
        expect(state?.unreadBySession).toEqual({ ses_b: 5 });
        expect(state?.totalUnread).toBe(5);
      });
    }
  });

  // Round 5 moved the demand decision into the reads; this states WHEN each read
  // asks it. Two things can make a response impossible or pointless to commit —
  // no reader left, and nowhere to put it — and both used to be answered somewhere
  // other than the moment of asking: the first once on the way into a read that
  // then issues many requests, the second only after a request had been paid for.
  // A read is not one request. `reconcileSessions` pages a window, the tree rebuild
  // issues one bootstrap per window size, and both go round again on a refused
  // response. So the property is per REQUEST: every request is preceded by the same
  // predicate, and a read that cannot commit anything issues nothing at all.
  describe('every request is preceded by the question, not every read', () => {
    it('stops paging a window between pages when its last reader leaves', async () => {
      const sessionB = { ...session, id: 'ses_b' };
      const bootstrap = vi.fn().mockResolvedValue({
        projects: [project],
        sessions: { proj_a: { sessions: [session, sessionB], next_before_id: null } },
      });
      const firstPage = deferred<{ sessions: Array<typeof session>; next_before_id: string | null }>();
      const listSessions = vi
        .fn()
        .mockReturnValueOnce(firstPage.promise)
        .mockResolvedValue({ sessions: [sessionB], next_before_id: null });
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        listSessions,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };
      let tree: WorkbenchProjectsTree | null = null;
      const captureTree = (next: WorkbenchProjectsTree) => {
        tree = next;
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(tree?.sessionsOf(project.id).sessions).toHaveLength(2);

      // Activity on a two-row window starts a reconcile that needs two pages.
      await act(async () => {
        handlers?.onSessionActivity?.({
          session_id: session.id,
          scope_id: session.scope_id,
          event: 'user_message',
        });
      });
      expect(listSessions).toHaveBeenCalledTimes(1);

      // Workbench → /admin between page one and page two.
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      await act(async () => {
        firstPage.resolve({ sessions: [session], next_before_id: 'cursor_2' });
      });
      await settle();

      // An entry gate passed before the navigation would have finished rebuilding a
      // window nobody renders, one page at a time.
      expect(listSessions).toHaveBeenCalledTimes(1);

      // And the abandoned half costs nothing: the window is still what it was, and
      // the returning consumer re-reads it.
      expect(tree?.sessionsOf(project.id).sessions).toHaveLength(2);
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(bootstrap).toHaveBeenCalledTimes(2);
    });

    it('stops rebuilding the tree between window-size groups', async () => {
      // The rebuild batches projects by how much of each window has to come back,
      // so it is one request per distinct size — a loop like any other.
      const projectWide = { ...project, id: 'proj_wide', scope_id: 'scope_wide', display_name: 'Wide' };
      const wideWindow = Array.from({ length: 10 }, (_, index) => ({
        ...session,
        id: `ses_wide_${index}`,
        project_id: projectWide.id,
        scope_id: projectWide.scope_id,
      }));
      const treePayload = {
        projects: [project, projectWide],
        sessions: {
          [project.id]: { sessions: [session], next_before_id: null },
          [projectWide.id]: { sessions: wideWindow, next_before_id: null },
        },
      };
      const firstGroup = deferred<typeof treePayload>();
      const bootstrap = vi
        .fn()
        .mockResolvedValueOnce(treePayload)
        .mockReturnValueOnce(firstGroup.promise)
        .mockResolvedValue(treePayload);
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        listSessions: vi.fn().mockResolvedValue({ sessions: [], next_before_id: null }),
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(bootstrap).toHaveBeenCalledTimes(1);

      await act(async () => {
        handlers?.onConnected?.({ sub_id: 1 });
      });
      expect(bootstrap).toHaveBeenCalledTimes(2);
      expect(bootstrap.mock.calls[1][0]).toMatchObject({ projectIds: [project.id] });

      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      await act(async () => {
        firstGroup.resolve(treePayload);
      });
      await settle();

      // The ten-row group is never asked for.
      expect(bootstrap).toHaveBeenCalledTimes(2);
    });

    it('stops a row refresh that a mid-flight event sent round again', async () => {
      // This read loops in place: an event landing while it is in flight refuses the
      // response and makes it read the row again. The second read is a request of
      // its own and asks the question of its own.
      const unbound = { ...session, native_session_id: null };
      const bootstrap = vi.fn().mockResolvedValue({
        projects: [project],
        sessions: { proj_a: { sessions: [unbound], next_before_id: null } },
      });
      const firstRead = deferred<typeof unbound>();
      const getSession = vi.fn().mockReturnValueOnce(firstRead.promise).mockResolvedValue(unbound);
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        getSession,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      await act(async () => {
        handlers?.onTurnEnd?.({ session_id: session.id });
      });
      expect(getSession).toHaveBeenCalledTimes(1);

      // A second turn-end refuses the read in flight and queues another pass.
      await act(async () => {
        handlers?.onTurnEnd?.({ session_id: session.id });
      });
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      await act(async () => {
        firstRead.resolve(unbound);
      });
      await settle();

      expect(getSession).toHaveBeenCalledTimes(1);
    });

    // The other half of the question. A populating read is exempt from demand, but
    // it still has to have somewhere to land — and it already knew the answer, since
    // it discarded every response that came back for a project the tree does not
    // hold. Asking first is the same question one round-trip earlier.
    it('issues no window read for a fork on a document that never loaded the tree', async () => {
      const forked = { ...session, id: 'ses_forked' };
      const forkSession = vi.fn().mockResolvedValue(forked);
      const listSessions = vi.fn().mockResolvedValue({ sessions: [forked], next_before_id: null });
      const bootstrap = vi.fn().mockResolvedValue(bootstrapPayload);
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        listSessions,
        forkSession,
        connectWorkbenchEvents: vi.fn(() => vi.fn()),
      };
      let actions: WorkbenchProjectsActions | null = null;
      const captureActions = (next: WorkbenchProjectsActions) => {
        actions = next;
      };

      // /chat/:id renders one session, never the tree — but it does fork.
      render(
        <WorkbenchProjectsProvider>
          <ActionsProbe onState={captureActions} />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      await act(async () => {
        await actions?.forkSession(project.id, session.id);
      });
      await settle();

      // The write is untouched; only the read that had nowhere to commit is gone.
      expect(forkSession).toHaveBeenCalledTimes(1);
      expect(listSessions).not.toHaveBeenCalled();
      expect(bootstrap).not.toHaveBeenCalled();
    });

    // The boundary that makes asking first safe. A write can CREATE the address in
    // the same tick it asks for the window, so membership has to be true when the
    // write returns rather than one render later — the list and its mirror are
    // committed together for exactly this.
    it('loads the window of a project the write it followed just added', async () => {
      const newProject = { ...project, id: 'proj_new', scope_id: 'scope_new', display_name: 'New' };
      const newSession = { ...session, id: 'ses_new', project_id: newProject.id, scope_id: newProject.scope_id };
      const bootstrap = vi.fn().mockResolvedValue({ projects: [project], sessions: {} });
      const listSessions = vi.fn().mockResolvedValue({ sessions: [newSession], next_before_id: null });
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        listSessions,
        connectWorkbenchEvents: vi.fn(() => vi.fn()),
      };
      let tree: WorkbenchProjectsTree | null = null;
      const captureTree = (next: WorkbenchProjectsTree) => {
        tree = next;
      };

      render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(tree?.projects).toEqual([project]);

      // Opening a tracked folder hoists the project and asks for its sessions in the
      // same statement.
      await act(async () => {
        tree?.upsertProjectToTop(newProject);
      });
      await settle();

      expect(listSessions).toHaveBeenCalledTimes(1);
      expect(listSessions.mock.calls[0][0]).toMatchObject({ projectId: newProject.id });
      expect(tree?.sessionsOf(newProject.id).sessions).toEqual([newSession]);
    });

    // The window read's own copy of the one demand decision a read cannot make for
    // itself: a populating read whose response a mutation refused re-queues ITSELF,
    // and that retry is a revalidation wearing the populating read's name. Same rule
    // as ``flushBootstrapReadIntent``, one level down.
    it('drops the window retry a mutation refused, once its reader is gone', async () => {
      const bootstrap = vi.fn().mockResolvedValue({ projects: [project], sessions: {} });
      const firstLoad = deferred<{ sessions: Array<typeof session>; next_before_id: string | null }>();
      const listSessions = vi
        .fn()
        .mockReturnValueOnce(firstLoad.promise)
        .mockResolvedValue({ sessions: [session], next_before_id: null });
      let handlers: WorkbenchEventHandlers | null = null;
      apiRef.current = {
        getWorkbenchProjectsBootstrap: bootstrap,
        listSessions,
        connectWorkbenchEvents: vi.fn((next) => {
          handlers = next;
          return vi.fn();
        }),
      };
      let tree: WorkbenchProjectsTree | null = null;
      const captureTree = (next: WorkbenchProjectsTree) => {
        tree = next;
      };

      const { rerender } = render(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      // Expanding a group populates its window — exempt from demand, and in flight.
      await act(async () => {
        tree?.toggleExpanded(project.id);
      });
      expect(listSessions).toHaveBeenCalledTimes(1);

      // Activity invalidates that read, so its response can no longer be committed.
      await act(async () => {
        handlers?.onSessionActivity?.({
          session_id: session.id,
          scope_id: session.scope_id,
          event: 'updated',
          title: 'Fresh title',
        });
      });
      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
        </WorkbenchProjectsProvider>,
      );
      await settle();

      await act(async () => {
        firstLoad.resolve({ sessions: [session], next_before_id: null });
      });
      await settle();

      // With a reader still there this retries (see WorkbenchSessionReadOrdering);
      // with none, the window it would keep filled has nobody to fill it for.
      expect(listSessions).toHaveBeenCalledTimes(1);

      rerender(
        <WorkbenchProjectsProvider>
          <TreeProbe active={false} onState={captureTree} />
          <TreeProbe />
        </WorkbenchProjectsProvider>,
      );
      await settle();
      expect(bootstrap).toHaveBeenCalledTimes(2);
    });
  });
});
