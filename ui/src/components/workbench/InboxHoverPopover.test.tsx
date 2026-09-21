/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { Popover } from '../ui/popover';
import type { InboxSession } from '../../context/ApiContext';
import { readInboxFilter } from '../../lib/inboxFilterMemory';
import { InboxHoverPopover } from './WorkbenchSidebar';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

// Newest first, the order the feed hands the popover.
const row = (id: number, minutesAgo: number): InboxSession => ({
  session_id: `session-${id}`, title: `Session ${id}`, scope_id: null,
  project_id: null, project_name: null,
  last_activity_at: new Date(Date.UTC(2026, 8, 21, 12, 0) - minutesAgo * 60_000).toISOString(),
  last_message_author: 'agent', replied: false, preview_text: `Preview ${id}`,
  preview_at: null, unread_count: 0, unread: false,
});

// `unreadSessions` counts the whole feed, so it is passed separately: the cases
// this popover has to get right are the ones where it exceeds what `sessions` holds.
function mount(
  sessions: InboxSession[],
  unreadBySession: Record<string, number>,
  unreadSessions = Object.keys(unreadBySession).length,
) {
  const peek = (
    <Popover open>
      <InboxHoverPopover
        sessions={sessions}
        unreadBySession={unreadBySession}
        unreadSessions={unreadSessions}
        totalUnread={Object.values(unreadBySession).reduce((sum, n) => sum + n, 0)}
        canMarkRead
        onItemClick={() => {}}
        onMarkAllRead={() => {}}
        onMouseEnter={() => {}}
        onMouseLeave={() => {}}
      />
    </Popover>
  );
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        {/* Stands in for the full Inbox page, so a link out of the peek is
            observable as the navigation it is meant to be. */}
        <Route path="/inbox" element={<p>Full inbox</p>} />
        <Route path="*" element={peek} />
      </Routes>
    </MemoryRouter>,
  );
  return screen
    .getAllByRole('button')
    .map((node) => node.textContent ?? '')
    .filter((text) => text.includes('Session '))
    .map((text) => text.match(/Session \d+/)?.[0] ?? '');
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

it('stacks unread sessions above read ones', () => {
  const sessions = [0, 1, 2, 3].map((id) => row(id, id * 10));
  expect(mount(sessions, { 'session-2': 1, 'session-3': 4 })).toEqual([
    'Session 2', 'Session 3', 'Session 0', 'Session 1',
  ]);
});

it('keeps the feed order inside each group', () => {
  const sessions = [0, 1, 2, 3].map((id) => row(id, id * 10));
  expect(mount(sessions, { 'session-0': 1, 'session-3': 1 })).toEqual([
    'Session 0', 'Session 3', 'Session 1', 'Session 2',
  ]);
});

it('shows an unread session the chronological five would have buried', () => {
  // Five read sessions are newer than the only unread one, so a plain slice of
  // the feed's first five never reaches it — which is the case the popover exists
  // to serve.
  const sessions = [0, 1, 2, 3, 4, 5].map((id) => row(id, id * 10));
  const shown = mount(sessions, { 'session-5': 2 });
  expect(shown).toHaveLength(5);
  expect(shown[0]).toBe('Session 5');
});

it('leaves the order alone when nothing is unread', () => {
  const sessions = [0, 1, 2].map((id) => row(id, id * 10));
  expect(mount(sessions, {})).toEqual(['Session 0', 'Session 1', 'Session 2']);
});

it('says so when an unread session sits outside the loaded pages', () => {
  // Five read rows loaded and three unread sessions counted feed-wide: two of the
  // three are older than the loaded window, so no ordering of these rows reaches
  // them and a header reading "3 unread" would otherwise sit above five read rows.
  const sessions = [0, 1, 2, 3, 4].map((id) => row(id, id * 10));
  mount(sessions, { 'session-4': 1 }, 3);
  const notice = screen.getByRole('button', { name: 'workbench.inbox.moreUnreadInFull' });
  fireEvent.click(notice);
  expect(screen.getByText('Full inbox')).toBeTruthy();
  // The full inbox resolves its tab from this store on entry, so the click has to
  // land the user on Unread rather than whichever tab they last left behind.
  expect(readInboxFilter()).toEqual({ tab: 'unread', leftAt: 0 });
});

it('stays quiet when the loaded rows already hold every unread session', () => {
  const sessions = [0, 1, 2].map((id) => row(id, id * 10));
  mount(sessions, { 'session-1': 1, 'session-2': 5 });
  expect(screen.queryByText('workbench.inbox.moreUnreadInFull')).toBeNull();
});

it('stays quiet when the peek is already full of unread sessions', () => {
  // Nothing is being hidden by the row limit that the header count does not
  // already state, so the notice would be noise on the common triage case.
  const sessions = [0, 1, 2, 3, 4, 5, 6].map((id) => row(id, id * 10));
  const unread = Object.fromEntries(sessions.map((s) => [s.session_id, 1]));
  expect(mount(sessions, unread)).toHaveLength(5);
  expect(screen.queryByText('workbench.inbox.moreUnreadInFull')).toBeNull();
});
