/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { InboxSession } from '../../context/ApiContext';
import type { InboxFeedStatus } from '../../context/WorkbenchInboxContext';
import { BotsContext, type BotsState } from '../../context/BotsContext';
import { writeInboxFilter } from '../../lib/inboxFilterMemory';
import { HANDOVER_CLASS } from '../ui/skeleton-handover';
import { InboxPage } from './InboxPage';

// The Inbox loads behind skeleton cards, never a spinner or an empty state it then
// takes back (ui/design: inbox/inbox-page/loading). Known rows (the local snapshot)
// show at once; the refresh icon spins only for a refresh the reader asked for.

const feed = vi.hoisted(() => ({
  inboxSessions: [] as InboxSession[],
  unreadBySession: {} as Record<string, number>,
  loading: true,
  feedStatus: 'pending' as InboxFeedStatus,
  refresh: vi.fn(),
}));
vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({
    ...feed,
    totalUnread: Object.values(feed.unreadBySession).reduce((sum, n) => sum + n, 0),
    unreadSessions: Object.keys(feed.unreadBySession).length,
    unreadByCategory: { bot: 0, session: 0 },
    nextCursor: null,
    loadingMore: false,
    loadMore: vi.fn(),
    markRead: vi.fn(),
  }),
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: true, can_read_instance: false } }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

const row = (id: string): InboxSession => ({
  session_id: id, title: id, scope_id: null, project_id: 'proj_a', project_name: 'avibe',
  last_activity_at: '2026-11-01T12:00:00Z', last_message_author: 'agent', replied: false,
  preview_text: `Reply in ${id}`, preview_at: null, unread_count: 1, unread: true,
});

const app = (bots = false) => (
  <MemoryRouter initialEntries={['/inbox']}>
    <BotsContext.Provider value={{ enabled: bots, bots: [], botForSession: () => undefined, displayName: () => '' } as unknown as BotsState}>
      <InboxPage onOpenSearch={() => {}} />
    </BotsContext.Provider>
  </MemoryRouter>
);
const cardsSkeleton = () => screen.queryAllByRole('status').filter((node) => node.querySelector(':scope > .skeleton-shimmer'));
const spinning = () => document.querySelectorAll('.animate-spin');

beforeEach(() => {
  writeInboxFilter('unread', 0);
  feed.inboxSessions = [];
  feed.unreadBySession = {};
  feed.loading = true;
  feed.feedStatus = 'pending';
  feed.refresh = vi.fn();
});
afterEach(cleanup);

describe('the Inbox first load', () => {
  it('shows skeleton cards, not an empty state, a zero count, or a spinner', () => {
    render(app());
    expect(cardsSkeleton()).toHaveLength(3);
    expect(screen.queryByText('workbench.inbox.allClearTitle')).toBeNull();
    expect(screen.queryByText('workbench.inbox.headerCount')).toBeNull();
    expect(spinning()).toHaveLength(0);
  });

  it('shows known rows at once, with no skeleton, while the live read is out', () => {
    feed.inboxSessions = [row('ses_a')];
    feed.unreadBySession = { ses_a: 1 };
    render(app());
    expect(screen.getByText('Reply in ses_a')).toBeTruthy();
    expect(cardsSkeleton()).toHaveLength(0);
    const list = document.querySelector('[data-inbox-session-id="ses_a"]')!.closest('section')!.parentElement!;
    expect(list.className).toContain('flex-col');
    expect(list.className).not.toContain('starting:opacity-0');
  });

  it('fades the first page in where the skeleton was', () => {
    const view = render(app());
    feed.inboxSessions = [row('ses_a')];
    feed.unreadBySession = { ses_a: 1 };
    feed.loading = false;
    feed.feedStatus = 'loaded';
    view.rerender(app());
    expect(cardsSkeleton()).toHaveLength(0);
    const list = document.querySelector('[data-inbox-session-id="ses_a"]')!.closest('section')!.parentElement!;
    expect(list.className).toContain(HANDOVER_CLASS);
  });

  it('says a failed first read failed and offers a retry, not a skeleton or an all-clear', () => {
    feed.loading = false;
    feed.feedStatus = 'failed';
    feed.refresh = vi.fn(() => Promise.resolve());
    render(app());
    expect(cardsSkeleton()).toHaveLength(0);
    expect(screen.queryAllByRole('status')).toHaveLength(0);
    expect(screen.queryByText('workbench.inbox.allClearTitle')).toBeNull();
    expect(screen.queryByText('workbench.inbox.headerCount')).toBeNull();
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('workbench.inbox.loadError');
    fireEvent.click(screen.getByRole('button', { name: 'common.retry' }));
    expect(feed.refresh).toHaveBeenCalledTimes(1);
  });

  it('spins the refresh icon only for the refresh the reader asked for', async () => {
    feed.inboxSessions = [row('ses_a')];
    feed.unreadBySession = { ses_a: 1 };
    feed.loading = false;
    feed.feedStatus = 'loaded';
    let finish!: () => void;
    feed.refresh = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(app());
    expect(spinning()).toHaveLength(0);
    fireEvent.click(screen.getAllByText('workbench.inbox.refresh')[0]);
    expect(spinning().length).toBeGreaterThan(0);
    await act(async () => { finish(); });
    expect(spinning()).toHaveLength(0);
  });

  it('with Bots, files the skeleton cards under the two section heads the page will show', () => {
    render(app(true));
    const region = screen.getAllByRole('status')[0];
    const heads = [...region.querySelectorAll('section > div:first-child')].map((head) => head.textContent);
    expect(heads).toEqual(['workbench.inbox.sections.bots', 'workbench.inbox.sections.sessions']);
    expect(cardsSkeleton()).toHaveLength(3);
  });
});
