// @vitest-environment jsdom

// A row's preview and its unread count must speak of the same reply. The count can
// move on its own: a push arriving while the event stream was down makes the page
// read the counts (`vibe.push-badge-refresh`), and that read brings no rows. The
// row shown was then read before its newest reply, so its preview is the message
// before the one the count stands for (owner report, 11-17). The row is read again.
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { InboxFeedResult, InboxSession, WorkbenchEventHandlers } from './ApiContext';
import { BotRosterContext } from './BotRosterContext';
import { NotificationPreferencesContext } from './NotificationPreferencesContext';
import { useWorkbenchInbox } from './WorkbenchInboxContext';
import { WorkbenchInboxProvider } from './WorkbenchInboxProvider';
import { DEFAULT_NOTIFICATION_PREFERENCES as preferences } from '../lib/notificationPreferences';

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

const row = (preview: string, unread: number): InboxSession => ({
  session_id: 'ses_a', title: 'A', scope_id: null, project_id: null, project_name: null,
  last_activity_at: '2026-11-17T06:00:00Z', last_message_author: 'agent', replied: false,
  preview_text: preview, preview_at: '2026-11-17T06:00:00Z', unread_count: unread, unread: unread > 0,
});

/** The server: its newest row for ses_a and the unread counts. */
const server = { row: row('Old reply', 0), unread: {} as Record<string, number> };
const listInbox = vi.fn(async (params?: { onlySession?: string; limit?: number }): Promise<InboxFeedResult> => ({
  // A counts read (limit 1, no session named) is answered with the counts; its row is not used.
  sessions: params?.onlySession || (params?.limit ?? 30) > 1 ? [server.row] : [],
  next_cursor: null,
  unread_by_session: { ...server.unread },
}));

const Probe = () => {
  const { inboxSessions, unreadBySession } = useWorkbenchInbox();
  return <ul>{inboxSessions.map((s) => <li key={s.session_id}>{`${s.preview_text}|${unreadBySession[s.session_id] ?? 0}`}</li>)}</ul>;
};

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
});

const worker = new EventTarget();
let handlers: WorkbenchEventHandlers | null = null;

beforeEach(() => {
  server.row = row('Old reply', 0);
  server.unread = {};
  apiRef.current = {
    listInbox,
    markSessionRead: vi.fn(async () => ({ updated: 0 })),
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers = next;
      return () => undefined;
    },
  };
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: worker });
  Object.defineProperty(navigator, 'setAppBadge', { configurable: true, value: vi.fn(async () => undefined) });
});
afterEach(() => {
  cleanup();
  Reflect.deleteProperty(navigator, 'serviceWorker');
  Reflect.deleteProperty(navigator, 'setAppBadge');
  vi.clearAllMocks();
});

const mount = () => render(
    <BotRosterContext.Provider value={{ roster: { sessionIds: new Set(), known: true }, publish: () => {} }}>
      <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update: async () => true }}>
        <WorkbenchInboxProvider><Probe /></WorkbenchInboxProvider>
      </NotificationPreferencesContext.Provider>
    </BotRosterContext.Provider>,
);
const pushRefresh = () => act(async () => {
  worker.dispatchEvent(new MessageEvent('message', { data: { type: 'vibe.push-badge-refresh' } }));
});

it('reads a row again when the counts move past it, so its preview is the unread reply', async () => {
  mount();
  await settle();
  expect(screen.getByRole('listitem').textContent).toBe('Old reply|0');

  // A new reply lands while the stream is down; the push makes the page read the counts.
  server.row = row('New reply', 1);
  server.unread = { ses_a: 1 };
  await pushRefresh();
  await settle();

  expect(screen.getByRole('listitem').textContent).toBe('New reply|1');
  expect(listInbox).toHaveBeenCalledWith(expect.objectContaining({ onlySession: 'ses_a' }));
});

it('sees a newer reply after the last one was read on another device', async () => {
  server.row = row('Old reply', 1);
  server.unread = { ses_a: 1 };
  mount();
  await settle();
  expect(screen.getByRole('listitem').textContent).toBe('Old reply|1');

  // Read on the Mac: the map drops to 0 here, the row keeps its old preview.
  await act(async () => { handlers?.onInboxUnreadChanged?.({ unread_by_session: {} }); });
  // Away, a new reply; the push reads the counts back to 1, the same number as before.
  server.row = row('New reply', 1);
  server.unread = { ses_a: 1 };
  await pushRefresh();
  await settle();

  expect(screen.getByRole('listitem').textContent).toBe('New reply|1');
});
