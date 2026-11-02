// @vitest-environment jsdom

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { InboxSession, WorkbenchEventHandlers } from './ApiContext';
import { BotRosterContext, type BotRoster } from './BotRosterContext';
import { NotificationPreferencesContext } from './NotificationPreferencesContext';
import { useWorkbenchInbox, type InboxState } from './WorkbenchInboxContext';
import { WorkbenchInboxProvider } from './WorkbenchInboxProvider';
import badgeFixture from '../lib/notificationBadge.fixture.json';
import type { NotificationPreferences } from '../lib/notificationPreferences';

// Which Sessions exist is the server's answer (docs/plans/bots.md D1): a removed
// Bot's Session is hidden, not an ordinary Session. These tests hold the client
// to that across the changes no Inbox event announces.

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

type ListInboxArgs = { limit?: number; before?: string; onlySession?: string };

const key = (row: InboxSession) => `${row.last_activity_at}|${row.session_id}`;
const minute = (n: number) => new Date(Date.UTC(2026, 10, 1, 8, 0) - n * 60_000).toISOString();
const row = (sessionId: string, minutesAgo: number, unread = 0): InboxSession => ({
  session_id: sessionId, scope_id: null, project_id: null, project_name: null, title: sessionId,
  last_activity_at: minute(minutesAgo), last_message_author: 'agent', replied: false,
  preview_text: `Reply in ${sessionId}`, preview_at: minute(minutesAgo), unread_count: unread, unread: unread > 0,
});

/** The server: the rows a reader may see, newest first, paged the way `GET /api/inbox` pages them. */
function fakeServer(initial: InboxSession[]) {
  const server = { rows: initial };
  const handlers: WorkbenchEventHandlers[] = [];
  const listInbox = vi.fn(async ({ limit = 30, before, onlySession }: ListInboxArgs) => {
    const sorted = [...server.rows].sort((a, b) => (key(a) < key(b) ? 1 : -1));
    const unread = Object.fromEntries(sorted.filter((r) => r.unread_count > 0).map((r) => [r.session_id, r.unread_count]));
    if (onlySession) {
      return { sessions: sorted.filter((r) => r.session_id === onlySession), next_cursor: null, unread_by_session: unread };
    }
    const window = sorted.filter((r) => before === undefined || key(r) < before).slice(0, limit);
    const next = window.length === limit ? key(window[window.length - 1]) : null;
    return { sessions: window, next_cursor: next, unread_by_session: unread };
  });
  apiRef.current = {
    listInbox,
    markSessionRead: vi.fn(async () => ({ updated: 0 })),
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  const emit = (fire: (handler: WorkbenchEventHandlers) => void) => act(async () => {
    for (const handler of handlers) fire(handler);
  });
  return { server, listInbox, emit };
}

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
});

function mount(roster: BotRoster, preferences: NotificationPreferences) {
  const state = { current: null as InboxState | null };
  const Probe = () => {
    const inbox = useWorkbenchInbox();
    useEffect(() => { state.current = inbox; });
    return null;
  };
  const tree = (bots: BotRoster) => (
    <BotRosterContext.Provider value={{ roster: bots, publish: () => {} }}>
      <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update: async () => true }}>
        <WorkbenchInboxProvider><Probe /></WorkbenchInboxProvider>
      </NotificationPreferencesContext.Provider>
    </BotRosterContext.Provider>
  );
  const view = render(tree(roster));
  return { state, rerender: (bots: BotRoster) => view.rerender(tree(bots)) };
}

const roster = (ids: string[]): BotRoster => ({ sessionIds: new Set(ids), known: true });
const ids = (state: { current: InboxState | null }) => state.current!.inboxSessions.map((r) => r.session_id);

const removal = badgeFixture.removed_bot;
const fixtureRows = (map: Record<string, number>) =>
  Object.entries(map).map(([sessionId, unread], index) => row(sessionId, index + 1, unread));

let setAppBadge: ReturnType<typeof vi.fn>;
let clearAppBadge: ReturnType<typeof vi.fn>;
beforeEach(() => {
  setAppBadge = vi.fn(async () => {});
  clearAppBadge = vi.fn(async () => {});
  Object.defineProperty(navigator, 'setAppBadge', { configurable: true, value: setAppBadge });
  Object.defineProperty(navigator, 'clearAppBadge', { configurable: true, value: clearAppBadge });
});
afterEach(() => {
  cleanup();
  Reflect.deleteProperty(navigator, 'setAppBadge');
  Reflect.deleteProperty(navigator, 'clearAppBadge');
});

describe('a Bot roster change', () => {
  it.each(removal.cases)('takes a removed Bot\'s Session out of the feed, the counts and the badge, as the server does ($name)', async (fixture) => {
    const { server, emit } = fakeServer(fixtureRows(removal.before.unread_by_session));
    const { state, rerender } = mount(roster(removal.before.bot_session_ids), fixture.preferences);
    await settle();
    expect(ids(state)).toContain(`ses_${removal.removed}`);

    // Removing the Bot hides its Session on the server; no Inbox event says so.
    server.rows = fixtureRows(removal.after.unread_by_session);
    await emit((handler) => handler.onBotsChanged?.({ handle: removal.removed }));
    rerender(roster(removal.after.bot_session_ids));
    await settle();

    expect(ids(state)).not.toContain(`ses_${removal.removed}`);
    expect(state.current!.unreadBySession).toEqual(removal.after.unread_by_session);
    expect(state.current!.unreadByCategory).toEqual(fixture.unread_by_category);
    // The server's badge_total for the same data (tests/test_notification_badge_parity.py).
    expect(state.current!.badgeTotal).toBe(fixture.badge_total);
    expect(setAppBadge).toHaveBeenLastCalledWith(fixture.badge_total);
  });

  it('brings a Session back when its Bot is set again', async () => {
    const { server, emit } = fakeServer(fixtureRows(removal.after.unread_by_session));
    const { state, rerender } = mount(roster(removal.after.bot_session_ids), removal.cases[0].preferences);
    await settle();
    expect(ids(state)).not.toContain(`ses_${removal.removed}`);

    server.rows = fixtureRows(removal.before.unread_by_session);
    await emit((handler) => handler.onBotsChanged?.({ handle: removal.removed }));
    rerender(roster(removal.before.bot_session_ids));
    await settle();

    expect(ids(state)).toContain(`ses_${removal.removed}`);
    expect(state.current!.unreadBySession).toEqual(removal.before.unread_by_session);
  });
});

describe('re-reading the window', () => {
  it('drops loaded rows the server no longer returns within the range it re-read, and keeps the rows below it', async () => {
    // Forty rows: page one and one "Load more".
    const original = Array.from({ length: 40 }, (_, index) => row(`sesr${index}`, 100 + index));
    const { server, emit } = fakeServer(original);
    const { state } = mount(roster([]), removal.cases[0].preferences);
    await settle();
    await act(async () => { await state.current!.loadMore(); });
    await settle();
    expect(ids(state)).toHaveLength(40);

    // While the stream was down two Sessions became hidden and five new ones
    // arrived, so the re-read of 40 rows ends at sesr36.
    const arrived = Array.from({ length: 5 }, (_, index) => row(`sesn${index}`, index + 1));
    server.rows = [...arrived, ...original.filter((r) => r.session_id !== 'sesr5' && r.session_id !== 'sesr35')];
    await emit((handler) => handler.onConnected?.());
    await settle();

    const shown = ids(state);
    expect(shown).not.toContain('sesr5');
    expect(shown).not.toContain('sesr35');
    // Below the re-read edge nothing was asked, so nothing is concluded.
    expect(shown.slice(-3)).toEqual(['sesr37', 'sesr38', 'sesr39']);
    expect(shown.slice(0, 5)).toEqual(['sesn0', 'sesn1', 'sesn2', 'sesn3', 'sesn4']);
  });
});
