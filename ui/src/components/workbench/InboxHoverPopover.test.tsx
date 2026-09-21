/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { Popover } from '../ui/popover';
import type { InboxSession } from '../../context/ApiContext';
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

function mount(sessions: InboxSession[], unreadBySession: Record<string, number>) {
  render(
    <MemoryRouter>
      <Popover open>
        <InboxHoverPopover
          sessions={sessions}
          unreadBySession={unreadBySession}
          unreadSessions={Object.keys(unreadBySession).length}
          totalUnread={Object.values(unreadBySession).reduce((sum, n) => sum + n, 0)}
          canMarkRead
          onItemClick={() => {}}
          onMarkAllRead={() => {}}
          onMouseEnter={() => {}}
          onMouseLeave={() => {}}
        />
      </Popover>
    </MemoryRouter>,
  );
  return screen
    .getAllByRole('button')
    .map((node) => node.textContent ?? '')
    .filter((text) => text.includes('Session '))
    .map((text) => text.match(/Session \d+/)?.[0] ?? '');
}

afterEach(cleanup);

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
