/* @vitest-environment jsdom */

import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { InboxSession } from '../../context/ApiContext';
import { writeInboxFilter } from '../../lib/inboxFilterMemory';
import { InboxPage } from './InboxPage';

// A live update draws again the card it is about, not the whole Inbox (#3256 kept the
// preview's Markdown to one card; each card also carries its motion now): every other
// card is handed a new but equal row and stays as it is. The minute moves every card's
// "3 minutes ago" on. Each card draws its time once per render, which is what is counted.

const relative = vi.hoisted(() => ({ calls: [] as string[] }));
vi.mock('../../lib/relativeTime', () => ({
  formatRelativeTime: (iso: string) => {
    relative.calls.push(iso);
    return iso;
  },
}));

// The provider's actions are stable callbacks, as the real one's are.
const feed = vi.hoisted(() => ({
  inboxSessions: [] as InboxSession[],
  unreadBySession: {} as Record<string, number>,
  actions: { refresh: () => Promise.resolve(), loadMore: () => Promise.resolve(), markRead: () => Promise.resolve(true) },
}));
vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({
    inboxSessions: feed.inboxSessions,
    unreadBySession: feed.unreadBySession,
    ...feed.actions,
    totalUnread: 0,
    unreadSessions: Object.keys(feed.unreadBySession).length,
    unreadByCategory: { bot: 0, session: 0 },
    nextCursor: null,
    feedStatus: 'loaded',
    loading: false,
    loadingMore: false,
  }),
}));
const authorization = vi.hoisted(() => ({ capabilities: { can_chat: true, can_read_instance: false } }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => authorization }));
// Stable across renders, as react-i18next's translator is until the language changes.
const i18n = vi.hoisted(() => ({ t: (key: string) => key }));
vi.mock('react-i18next', () => ({ useTranslation: () => i18n }));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

const row = (id: string, at: string): InboxSession => ({
  session_id: id, title: id, scope_id: null, project_id: 'proj_a', project_name: 'avibe',
  last_activity_at: at, last_message_author: 'agent', replied: false,
  preview_text: `Reply in ${id}`, preview_at: null, unread_count: 1, unread: true,
});
const page = () => (
  <MemoryRouter initialEntries={['/inbox']}>
    <InboxPage onOpenSearch={() => {}} />
  </MemoryRouter>
);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'], now: Date.UTC(2026, 10, 6, 8, 0, 0) });
  writeInboxFilter('all', 0);
  feed.inboxSessions = ['a', 'b', 'c'].map((id) => row(id, `2026-11-06T07:5${id.charCodeAt(0) - 97}:00Z`));
  feed.unreadBySession = { a: 1, b: 1, c: 1 };
  relative.calls = [];
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('Inbox cards', () => {
  it('draws again only the card a live update is about', () => {
    const view = render(page());
    expect(relative.calls).toHaveLength(3);
    relative.calls = [];
    // b gets a reply and moves to the top; a and c arrive as new, equal objects.
    feed.inboxSessions = [
      { ...row('b', '2026-11-06T08:00:00Z'), preview_text: 'A new reply' },
      { ...feed.inboxSessions[0] },
      { ...feed.inboxSessions[2] },
    ];
    feed.unreadBySession = { ...feed.unreadBySession, b: 2 };
    view.rerender(page());
    expect(relative.calls).toEqual(['2026-11-06T08:00:00Z']);
  });

  it('moves every card\'s time on as the minute turns', () => {
    render(page());
    relative.calls = [];
    act(() => {
      vi.setSystemTime(Date.UTC(2026, 10, 6, 8, 1, 0));
      vi.advanceTimersByTime(15_000);
    });
    expect(relative.calls).toHaveLength(3);
  });
});
