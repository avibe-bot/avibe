// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { InboxFeedResult, InboxSession, WorkbenchEventHandlers } from './ApiContext';
import { BotRosterContext } from './BotRosterContext';
import { InstanceAuthorizationProvider } from './InstanceAuthorizationProvider';
import { NotificationPreferencesContext } from './NotificationPreferencesContext';
import { useWorkbenchInbox, type InboxState } from './WorkbenchInboxContext';
import { WorkbenchInboxProvider } from './WorkbenchInboxProvider';
import {
  clearLocalSnapshots,
  readLocalSnapshot,
  setLocalSnapshotPrincipal,
  writeLocalSnapshot,
} from '../lib/localSnapshots';
import { DEFAULT_NOTIFICATION_PREFERENCES as preferences } from '../lib/notificationPreferences';

// A cold start paints the Inbox from the last snapshot (docs/plans/
// web-ui-local-cache.md) and the live read then owns it. The risks are the two
// directions of that hand-over: a snapshot row surviving the live answer that
// left it out, and the cached counts reaching the app-icon badge as if the server
// had said them.

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

const row = (sessionId: string, minutesAgo: number, unread = 0): InboxSession => ({
  session_id: sessionId, scope_id: null, project_id: null, project_name: null, title: sessionId,
  last_activity_at: new Date(Date.UTC(2026, 10, 1, 8, 0) - minutesAgo * 60_000).toISOString(),
  last_message_author: 'agent', replied: false, preview_text: `Reply in ${sessionId}`, preview_at: null,
  unread_count: unread, unread: unread > 0,
});

function mount(authorId = 'local') {
  const state = { current: null as InboxState | null };
  const Probe = () => {
    const inbox = useWorkbenchInbox();
    useEffect(() => { state.current = inbox; });
    return null;
  };
  const tree = (reader: string) => (
    <BotRosterContext.Provider value={{ roster: { sessionIds: new Set(), known: true }, publish: () => {} }}>
      <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update: async () => true }}>
        <WorkbenchInboxProvider>
          <InstanceAuthorizationProvider session={{ remote: false, instance_kind: null, author_id: reader }}>
            <Probe />
          </InstanceAuthorizationProvider>
        </WorkbenchInboxProvider>
      </NotificationPreferencesContext.Provider>
    </BotRosterContext.Provider>
  );
  const view = render(tree(authorId));
  Object.assign(state, { switchReader: (reader: string) => view.rerender(tree(reader)) });
  return state as typeof state & { switchReader: (reader: string) => void };
}

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
});

let setAppBadge: ReturnType<typeof vi.fn>;
beforeEach(() => {
  setAppBadge = vi.fn(async () => {});
  Object.defineProperty(navigator, 'setAppBadge', { configurable: true, value: setAppBadge });
  Object.defineProperty(navigator, 'clearAppBadge', { configurable: true, value: vi.fn(async () => {}) });
});
afterEach(async () => {
  cleanup();
  Reflect.deleteProperty(navigator, 'setAppBadge');
  Reflect.deleteProperty(navigator, 'clearAppBadge');
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('the Inbox local snapshot', () => {
  it('paints the last feed before the live read, then lets the live read replace it and stores that', async () => {
    setLocalSnapshotPrincipal('local');
    await writeLocalSnapshot('inbox:first-page', 1, {
      sessions: [row('ses_kept', 1, 2), row('ses_gone', 2, 1)],
      unread: { ses_kept: 2, ses_gone: 1 },
    });
    setLocalSnapshotPrincipal(null);

    let answer: (result: InboxFeedResult) => void = () => {};
    apiRef.current = {
      listInbox: vi.fn(() => new Promise<InboxFeedResult>((resolve) => { answer = resolve; })),
      markSessionRead: vi.fn(async () => ({ updated: 0 })),
      connectWorkbenchEvents: (_handlers: WorkbenchEventHandlers) => () => undefined,
    };
    const state = mount();
    await settle();

    // The server has not answered yet: the snapshot is on screen, the badge is not.
    expect(state.current!.inboxSessions.map((s) => s.session_id)).toEqual(['ses_kept', 'ses_gone']);
    expect(state.current!.totalUnread).toBe(3);
    expect(state.current!.unreadLoaded).toBe(false);
    expect(setAppBadge).not.toHaveBeenCalled();

    // The server no longer returns ses_gone (archived, hidden, or revoked).
    await act(async () => {
      answer({ sessions: [row('ses_new', 0, 1), row('ses_kept', 1, 0)], next_cursor: null, unread_by_session: { ses_new: 1 } });
    });
    await settle();
    expect(state.current!.inboxSessions.map((s) => s.session_id)).toEqual(['ses_new', 'ses_kept']);
    expect(state.current!.unreadBySession).toEqual({ ses_new: 1 });
    expect(setAppBadge).toHaveBeenLastCalledWith(1);

    // The live answer becomes the next cold start's snapshot.
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    Reflect.deleteProperty(document, 'visibilityState');
    await settle();
    const stored = await readLocalSnapshot('inbox:first-page', 1, (value) => value as {
      sessions: InboxSession[]; unread: Record<string, number>;
    });
    expect(stored?.sessions.map((s) => s.session_id)).toEqual(['ses_new', 'ses_kept']);
    expect(stored?.unread).toEqual({ ses_new: 1 });
  });

  it('takes the cursor from the first live answer when the snapshot is all the window held', async () => {
    setLocalSnapshotPrincipal('local');
    await writeLocalSnapshot('inbox:first-page', 1, { sessions: [row('ses_a', 1), row('ses_b', 2)], unread: {} });
    setLocalSnapshotPrincipal(null);

    let failFirstRead: (error: Error) => void = () => {};
    const listInbox = vi.fn()
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { failFirstRead = reject; }))
      .mockResolvedValue({
        sessions: [row('ses_a', 1), row('ses_b', 2)],
        next_cursor: 'cursor-after-b',
        unread_by_session: {},
      });
    const handlers: WorkbenchEventHandlers[] = [];
    apiRef.current = {
      listInbox,
      markSessionRead: vi.fn(async () => ({ updated: 0 })),
      connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
        handlers.push(next);
        return () => undefined;
      },
    };
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const state = mount();
    await settle();
    expect(state.current!.inboxSessions.map((s) => s.session_id)).toEqual(['ses_a', 'ses_b']);
    expect(listInbox).toHaveBeenCalledTimes(1);

    // The page-one read fails with only the snapshot on screen. When the stream
    // comes back the window is reconciled; its rows overlap the snapshot, but the
    // snapshot never had a cursor to keep.
    await act(async () => { failFirstRead(new Error('offline')); });
    await settle();
    await act(async () => { for (const handler of handlers) handler.onConnected?.(); });
    await settle();
    expect(listInbox).toHaveBeenCalledTimes(2);
    expect(state.current!.nextCursor).toBe('cursor-after-b');
    consoleError.mockRestore();
  });

  it('drops a painted snapshot the moment a different reader is confirmed, before any live answer', async () => {
    setLocalSnapshotPrincipal('remote:alice');
    await writeLocalSnapshot('inbox:first-page', 1, {
      sessions: [row('ses_alice', 1, 2)],
      unread: { ses_alice: 2 },
    });
    setLocalSnapshotPrincipal(null);

    apiRef.current = {
      // The live read never answers: whatever is on screen must not be Alice's.
      listInbox: vi.fn(() => new Promise(() => {})),
      markSessionRead: vi.fn(async () => ({ updated: 0 })),
      connectWorkbenchEvents: () => () => undefined,
    };
    // A cold start replays Alice's session...
    const state = mount('remote:alice');
    await settle();
    expect(state.current!.inboxSessions.map((s) => s.session_id)).toEqual(['ses_alice']);
    expect(state.current!.totalUnread).toBe(2);

    // ...and the guard's check confirms Bob.
    state.switchReader('remote:bob');
    await settle();
    expect(state.current!.inboxSessions).toEqual([]);
    expect(state.current!.unreadBySession).toEqual({});
  });

  it('stores the next live read for a reader confirmed after the feed went live, and nothing before it', async () => {
    const page = (id: string): InboxFeedResult => ({ sessions: [row(id, 0, 1)], next_cursor: null, unread_by_session: { [id]: 1 } });
    const listInbox = vi.fn(async () => page('ses_first'));
    apiRef.current = {
      listInbox,
      markSessionRead: vi.fn(async () => ({ updated: 0 })),
      connectWorkbenchEvents: () => () => undefined,
    };
    const hide = async () => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
      await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
      Reflect.deleteProperty(document, 'visibilityState');
      await settle();
    };
    const stored = () => readLocalSnapshot('inbox:first-page', 1, (value) => value as { sessions: InboxSession[] });

    // A cold start replays Alice's session, and the live read lands before the
    // guard's check has answered.
    const state = mount('remote:alice');
    await settle();
    expect(state.current!.inboxSessions.map((s) => s.session_id)).toEqual(['ses_first']);
    await hide();
    expect((await stored())?.sessions.map((s) => s.session_id)).toEqual(['ses_first']);

    // The check confirms Bob. The feed on screen was committed for Alice, so it
    // is not stored as Bob's...
    state.switchReader('remote:bob');
    await settle();
    await hide();
    expect(await stored()).toBeNull();

    // ...and Bob's own next live read is.
    listInbox.mockImplementation(async () => page('ses_bob'));
    await act(async () => { await state.current!.refresh(); });
    await settle();
    await hide();
    expect((await stored())?.sessions.map((s) => s.session_id)).toEqual(['ses_bob']);
  });
});
