// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { InboxFeedResult, InboxSession, WorkbenchEventHandlers } from './ApiContext';
import { BotRosterContext } from './BotRosterContext';
import { InstanceAuthorizationProvider } from './InstanceAuthorizationProvider';
import { NotificationPreferencesContext } from './NotificationPreferencesContext';
import { useWorkbenchInbox, type InboxState } from './WorkbenchInboxContext';
import { WorkbenchInboxProvider } from './WorkbenchInboxProvider';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../lib/localSnapshots';
import { DEFAULT_NOTIFICATION_PREFERENCES as preferences } from '../lib/notificationPreferences';

// `feedStatus` is how a consumer with no rows tells "the feed's first answer is
// still owed" (a skeleton) from "it failed" (a retry), so a failed read is never
// drawn as work still going on. It belongs to the window: once answered it stays
// answered, and a window dropped for a change of reader starts over.

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

const row = (sessionId: string): InboxSession => ({
  session_id: sessionId, scope_id: null, project_id: null, project_name: null, title: sessionId,
  last_activity_at: '2026-11-01T08:00:00Z', last_message_author: 'agent', replied: false,
  preview_text: `Reply in ${sessionId}`, preview_at: null, unread_count: 1, unread: true,
});
const page = (id: string): InboxFeedResult => ({ sessions: [row(id)], next_cursor: null, unread_by_session: { [id]: 1 } });

/** One controllable read per call: the test answers or fails each in turn. */
function reads() {
  const pending: { resolve: (result: InboxFeedResult) => void; reject: (error: Error) => void }[] = [];
  const listInbox = vi.fn(
    () => new Promise<InboxFeedResult>((resolve, reject) => { pending.push({ resolve, reject }); }),
  );
  return {
    listInbox,
    next: () => pending.shift()!,
    /** Answer every read still out (a counts-only read may sit before the feed's). */
    answerAll: (result: InboxFeedResult) => { for (const read of pending.splice(0)) read.resolve(result); },
    failAll: () => { for (const read of pending.splice(0)) read.reject(new Error('offline')); },
  };
}

function mount(listInbox: () => Promise<InboxFeedResult>) {
  const handlers: WorkbenchEventHandlers[] = [];
  apiRef.current = {
    listInbox,
    markSessionRead: vi.fn(async () => ({ updated: 0 })),
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  const state = { current: null as InboxState | null };
  // `feed`: whether the probe renders the feed (activating its read) or only reads the state.
  const Probe = ({ feed }: { feed: boolean }) => {
    const inbox = useWorkbenchInbox({ feed });
    useEffect(() => { state.current = inbox; });
    return null;
  };
  const tree = (feed: boolean) => (
    <BotRosterContext.Provider value={{ roster: { sessionIds: new Set(), known: true }, publish: () => {} }}>
      <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update: async () => true }}>
        <WorkbenchInboxProvider>
          <InstanceAuthorizationProvider session={{ remote: false, instance_kind: null, author_id: 'local' }}>
            <Probe feed={feed} />
          </InstanceAuthorizationProvider>
        </WorkbenchInboxProvider>
      </NotificationPreferencesContext.Provider>
    </BotRosterContext.Provider>
  );
  const view = render(tree(true));
  const emit = (event: keyof WorkbenchEventHandlers) =>
    act(async () => { for (const handler of handlers) (handler[event] as (() => void) | undefined)?.(); });
  return { state, emit, showFeed: (feed: boolean) => view.rerender(tree(feed)) };
}

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
});

afterEach(async () => {
  cleanup();
  vi.restoreAllMocks();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('the Inbox feed status', () => {
  it('says a failed first read failed, and owes an answer again while the next read is out', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const feed = reads();
    const { state, emit } = mount(feed.listInbox);
    await settle();
    expect(state.current!.feedStatus).toBe('pending');

    await act(async () => { feed.next().reject(new Error('offline')); });
    await settle();
    expect(state.current!.inboxSessions).toEqual([]);
    expect(state.current!.loading).toBe(false);
    expect(state.current!.feedStatus).toBe('failed');

    // The stream comes back: the window is read again, and is owed until it answers.
    await emit('onConnected');
    await settle();
    expect(feed.listInbox).toHaveBeenCalledTimes(2);
    expect(state.current!.feedStatus).toBe('pending');
    await act(async () => { feed.next().resolve(page('ses_a')); });
    await settle();
    expect(state.current!.feedStatus).toBe('loaded');
    expect(state.current!.inboxSessions.map((s) => s.session_id)).toEqual(['ses_a']);
  });

  it('retries a failed first read through refresh', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const feed = reads();
    const { state } = mount(feed.listInbox);
    await settle();
    await act(async () => { feed.next().reject(new Error('offline')); });
    await settle();
    expect(state.current!.feedStatus).toBe('failed');

    let retried!: Promise<void>;
    act(() => { retried = state.current!.refresh(); });
    await settle();
    expect(state.current!.feedStatus).toBe('pending');
    await act(async () => { feed.next().resolve(page('ses_a')); await retried; });
    await settle();
    expect(state.current!.feedStatus).toBe('loaded');
  });

  it('stays answered when a later read of a known feed fails', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const feed = reads();
    const { state, emit } = mount(feed.listInbox);
    await settle();
    await act(async () => { feed.next().resolve(page('ses_a')); });
    await settle();
    expect(state.current!.feedStatus).toBe('loaded');

    await emit('onConnected');
    await settle();
    expect(state.current!.feedStatus).toBe('loaded');
    await act(async () => { feed.next().reject(new Error('offline')); });
    await settle();
    expect(state.current!.feedStatus).toBe('loaded');
    expect(state.current!.inboxSessions.map((s) => s.session_id)).toEqual(['ses_a']);
  });

  it('starts over when a change of authorization drops the window, even with no feed on screen', async () => {
    const feed = reads();
    const { state, emit, showFeed } = mount(feed.listInbox);
    await settle();
    await act(async () => { feed.next().resolve(page('ses_a')); });
    await settle();
    expect(state.current!.feedStatus).toBe('loaded');

    // Nothing renders the feed when the change lands, so no read follows it: a feed
    // shown later must find the window owed, not an answered (and empty) one.
    showFeed(false);
    await settle();
    await emit('onAuthorizationChanged');
    await settle();
    expect(state.current!.inboxSessions).toEqual([]);
    expect(state.current!.feedStatus).toBe('pending');

    showFeed(true);
    await settle();
    expect(state.current!.feedStatus).toBe('pending');
    await act(async () => { feed.answerAll(page('ses_b')); });
    await settle();
    await act(async () => { feed.answerAll(page('ses_b')); });
    await settle();
    expect(state.current!.feedStatus).toBe('loaded');
    expect(state.current!.inboxSessions.map((s) => s.session_id)).toEqual(['ses_b']);
  });

  it('says so when the first read after a dropped window fails', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const feed = reads();
    const { state, emit } = mount(feed.listInbox);
    await settle();
    await act(async () => { feed.next().resolve(page('ses_a')); });
    await settle();

    await emit('onAuthorizationChanged');
    await settle();
    expect(state.current!.feedStatus).toBe('pending');
    await act(async () => { feed.failAll(); });
    await settle();
    expect(state.current!.inboxSessions).toEqual([]);
    expect(state.current!.feedStatus).toBe('failed');
  });
});
