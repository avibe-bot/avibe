/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import type { InboxSession } from '../../context/ApiContext';
import { BotsContext, type BotsState } from '../../context/BotsContext';
import { BotRosterContext } from '../../context/BotRosterContext';
import { NotificationPreferencesContext } from '../../context/NotificationPreferencesContext';
import type { Bot } from '../../lib/bots';
import { writeInboxFilter } from '../../lib/inboxFilterMemory';
import { unreadByCategory, type NotificationPreferences } from '../../lib/notificationPreferences';
import { InboxPage } from './InboxPage';

const feed = vi.hoisted(() => ({
  inboxSessions: [] as InboxSession[],
  unreadBySession: {} as Record<string, number>,
  markRead: vi.fn(async () => true),
}));
vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({
    ...feed,
    totalUnread: Object.values(feed.unreadBySession).reduce((sum, n) => sum + n, 0),
    unreadSessions: Object.keys(feed.unreadBySession).length,
    unreadByCategory: unreadByCategory(feed.unreadBySession, new Set(['ses_cso'])),
    nextCursor: null,
    loading: false,
    loadingMore: false,
    refresh: vi.fn(),
    loadMore: vi.fn(),
  }),
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: true, can_read_instance: false } }),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { mode?: string }) => (options?.mode ? `${key}:${options.mode}` : key),
  }),
}));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

const bots: Bot[] = [
  { handle: 'cso', display_name: 'CSO', avatar: null, role: '', agent_name: 'cso', session_id: 'ses_cso', position: 1, builtin: false },
];
const botsState = {
  enabled: true,
  bots,
  botForSession: (id: string | null | undefined) => bots.find((bot) => bot.session_id === id),
  displayName: (bot: Bot) => bot.display_name,
} as unknown as BotsState;

const row = (sessionId: string, title: string | null): InboxSession => ({
  session_id: sessionId, title, scope_id: null, project_id: 'proj_a', project_name: 'avibe',
  last_activity_at: '2026-11-01T12:00:00Z', last_message_author: 'agent', replied: false,
  preview_text: `Reply in ${sessionId}`, preview_at: null, unread_count: 0, unread: false,
});

function mount(preferences: NotificationPreferences = { enabled: true, bots: true, sessions: true }) {
  render(
    <MemoryRouter initialEntries={['/inbox']}>
      <BotRosterContext.Provider value={{ roster: { sessionIds: new Set(['ses_cso']), known: true }, publish: () => {} }}>
        <BotsContext.Provider value={botsState}>
          <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update: async () => true }}>
            <InboxPage onOpenSearch={() => {}} />
          </NotificationPreferencesContext.Provider>
        </BotsContext.Provider>
      </BotRosterContext.Provider>
    </MemoryRouter>,
  );
}

const sectionOf = (name: string) => screen.getByText(name).closest('section')!;

beforeEach(() => {
  writeInboxFilter('all', 0);
  feed.inboxSessions = [row('sesa1', 'Fix cache'), row('ses_cso', null), row('sesb2', 'Audit'), row('sesc3', 'Billing')];
  feed.unreadBySession = { ses_cso: 2, sesa1: 1, sesb2: 3 };
  feed.markRead.mockClear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('Inbox page with Bots', () => {
  it('shows Bots above Sessions, each in the feed order', () => {
    mount();

    const sections = Array.from(document.querySelectorAll('section')).map((section) =>
      Array.from(section.querySelectorAll('[data-inbox-session-id]')).map((node) => node.getAttribute('data-inbox-session-id')),
    );
    expect(sections).toEqual([['ses_cso'], ['sesa1', 'sesb2', 'sesc3']]);
    // The section says Bot already; its card carries no Bot tag.
    expect(within(sectionOf('workbench.inbox.sections.bots')).queryByText('bots.tag')).toBeNull();
  });

  it('marks only the Sessions section read from its header', () => {
    mount();

    fireEvent.click(within(sectionOf('workbench.inbox.sections.sessions')).getByRole('button', { name: /workbench.inbox.sections.markRead/ }));

    expect(feed.markRead.mock.calls.map(([id]) => id).sort()).toEqual(['sesa1', 'sesb2']);
  });

  it('shows the mode on its Notifications button, mutes the quiet section, and opens the switches', () => {
    mount({ enabled: true, bots: true, sessions: false });

    expect(within(sectionOf('workbench.inbox.sections.sessions')).getByText('workbench.inbox.sections.muted')).toBeTruthy();
    expect(within(sectionOf('workbench.inbox.sections.bots')).queryByText('workbench.inbox.sections.muted')).toBeNull();

    const button = screen.getByRole('button', { name: /workbench.notifications.button/ });
    expect(button.textContent).toContain('workbench.notifications.mode.bots');
    fireEvent.click(button);
    expect(screen.getAllByRole('switch').map((node) => [node.getAttribute('aria-label'), node.getAttribute('aria-checked')])).toEqual([
      ['workbench.notifications.title', 'true'],
      ['workbench.notifications.bots', 'true'],
      ['workbench.notifications.sessions', 'false'],
    ]);
  });
});
