/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import type { InboxSession } from '../../context/ApiContext';
import { writeInboxFilter } from '../../lib/inboxFilterMemory';
import { InboxPage } from './InboxPage';

const feed = vi.hoisted(() => ({
  inboxSessions: [] as InboxSession[],
  unreadBySession: {} as Record<string, number>,
  loading: false,
  nextCursor: null as string | null,
  refresh: vi.fn(),
  loadMore: vi.fn(),
  markRead: vi.fn(),
}));
vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({
    ...feed,
    totalUnread: Object.values(feed.unreadBySession).reduce((sum, n) => sum + n, 0),
    unreadSessions: Object.keys(feed.unreadBySession).length,
    loadingMore: false,
  }),
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: true, can_read_instance: false } }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

const row = (id: number): InboxSession => ({
  session_id: `session-${id}`, title: `Session ${id}`, scope_id: null,
  project_id: null, project_name: null, last_activity_at: '2026-09-21T12:00:00Z',
  last_message_author: 'agent', replied: false, preview_text: `Reply ${id}`,
  preview_at: null, unread_count: 0, unread: false,
});

// Stands in for any caller that records the tab it wants and then navigates to
// /inbox — the sidebar peek's "unread is outside this preview" link does exactly
// this. Rendered beside the page so the navigation lands on the route the page
// is already mounted on, which is the case a fresh mount never exercises.
function ReEnter() {
  const navigate = useNavigate();
  return <button onClick={() => { writeInboxFilter('unread', 0); navigate('/inbox'); }}>Re-enter</button>;
}

const visibleRows = () => document.querySelectorAll('[data-inbox-session-id]').length;

function mount() {
  render(
    <MemoryRouter initialEntries={['/inbox']}>
      <Routes>
        <Route path="/inbox" element={<><InboxPage onOpenSearch={() => {}} /><ReEnter /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

it('honours a tab recorded just before navigating to the inbox it is already on', () => {
  feed.inboxSessions = [0, 1, 2].map(row);
  feed.unreadBySession = { 'session-1': 1 };
  writeInboxFilter('all', 0);
  mount();
  expect(visibleRows()).toBe(3);

  fireEvent.click(screen.getByText('Re-enter'));
  // Same route, so React Router keeps this page mounted and the mount-time
  // resolve never runs again. Re-entering still has to mean what entering means.
  expect(visibleRows()).toBe(1);
});
