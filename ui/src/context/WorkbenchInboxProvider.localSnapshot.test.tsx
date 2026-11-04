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

function mount() {
  const state = { current: null as InboxState | null };
  const Probe = () => {
    const inbox = useWorkbenchInbox();
    useEffect(() => { state.current = inbox; });
    return null;
  };
  render(
    <BotRosterContext.Provider value={{ roster: { sessionIds: new Set(), known: true }, publish: () => {} }}>
      <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update: async () => true }}>
        <WorkbenchInboxProvider>
          <InstanceAuthorizationProvider session={{ remote: false, instance_kind: null, author_id: 'local' }}>
            <Probe />
          </InstanceAuthorizationProvider>
        </WorkbenchInboxProvider>
      </NotificationPreferencesContext.Provider>
    </BotRosterContext.Provider>,
  );
  return state;
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
});
