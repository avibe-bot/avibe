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
import { useWorkbenchProjectsTree } from './WorkbenchProjectsContext';
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

const TreeProbe = () => {
  useWorkbenchProjectsTree();
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
});
