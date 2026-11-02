/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { Popover } from '../ui/popover';
import type { InboxSession } from '../../context/ApiContext';
import { readInboxFilter } from '../../lib/inboxFilterMemory';
import { InboxHoverPopover } from './WorkbenchSidebar';
import { BotsContext, type BotsState } from '../../context/BotsContext';
import { BotRosterContext } from '../../context/BotRosterContext';
import { NotificationPreferencesContext } from '../../context/NotificationPreferencesContext';
import type { Bot } from '../../lib/bots';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

// Each row renders its activity time relative to `Date.now()`, and the title is
// read out of the same button text, so the clock is pinned to the instant the
// fixture counts back from. Left on the wall clock, a week later the label turns
// into a calendar date and its digits run into the title.
const NOW = Date.UTC(2026, 8, 21, 12, 0);

// Newest first, the order the feed hands the popover.
const row = (id: number, minutesAgo: number): InboxSession => ({
  session_id: `session-${id}`, title: `Session ${id}`, scope_id: null,
  project_id: null, project_name: null,
  last_activity_at: new Date(NOW - minutesAgo * 60_000).toISOString(),
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

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ['Date'] });
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.useRealTimers();
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

describe('with Bots', () => {
  const bots: Bot[] = [
    { handle: 'main', display_name: 'Vibey', avatar: { kind: 'mascot' }, role: '', agent_name: 'main', session_id: 'ses_main', position: 0, builtin: true, hide_details: true },
    { handle: 'cso', display_name: 'CSO', avatar: null, role: '', agent_name: 'cso', session_id: 'ses_cso', position: 1, builtin: false, hide_details: true },
  ];
  const botsState = {
    enabled: true,
    bots,
    botForSession: (id: string | null | undefined) => bots.find((bot) => bot.session_id === id),
    displayName: (bot: Bot) => bot.display_name,
  } as unknown as BotsState;
  const botRow = (sessionId: string, minutesAgo: number): InboxSession => ({ ...row(0, minutesAgo), session_id: sessionId, title: null });

  function mountWithBots(
    sessions: InboxSession[],
    unreadBySession: Record<string, number>,
    preferences = { enabled: true, bots: true, sessions: true },
  ) {
    const roster = { sessionIds: new Set(bots.map((bot) => bot.session_id)), known: true };
    render(
      <MemoryRouter initialEntries={['/']}>
        <BotRosterContext.Provider value={{ roster, publish: () => {} }}>
          <BotsContext.Provider value={botsState}>
            <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update: async () => true }}>
              <Popover open>
                <InboxHoverPopover
                  sessions={sessions}
                  unreadBySession={unreadBySession}
                  unreadSessions={Object.values(unreadBySession).filter((n) => n > 0).length}
                  totalUnread={Object.values(unreadBySession).reduce((sum, n) => sum + n, 0)}
                  canMarkRead
                  onItemClick={() => {}}
                  onMarkAllRead={() => {}}
                  onMouseEnter={() => {}}
                  onMouseLeave={() => {}}
                />
              </Popover>
            </NotificationPreferencesContext.Provider>
          </BotsContext.Provider>
        </BotRosterContext.Provider>
      </MemoryRouter>,
    );
  }
  const rowNames = () => screen
    .getAllByRole('button')
    .map((node) => node.textContent ?? '')
    .map((text) => text.match(/Session \d+|Vibey|CSO/)?.[0])
    .filter(Boolean);

  it('puts Bots above Sessions, three rows each and unread first', () => {
    const sessions = [botRow('ses_main', 1), row(1, 2), row(2, 3), botRow('ses_cso', 4), row(3, 5), row(4, 6)];
    mountWithBots(sessions, { ses_cso: 2, 'session-4': 1 });

    const headers = screen.getAllByText(/workbench\.inbox\.sections\.(bots|sessions)/).map((node) => node.textContent);
    expect(headers).toEqual(['workbench.inbox.sections.bots', 'workbench.inbox.sections.sessions']);
    expect(rowNames()).toEqual(['CSO', 'Vibey', 'Session 4', 'Session 1', 'Session 2']);
    // The section says Bot already; its rows carry no Bot tag.
    expect(screen.queryByText('bots.tag')).toBeNull();
  });

  it('files a Session that is not on the Bot list with the Sessions, whatever its id', () => {
    // The live list decides, as on the server; an id pattern does not.
    mountWithBots([botRow('ses_cso', 1), { ...row(7, 2), session_id: 'ses_ordinary' }], { ses_cso: 1, ses_ordinary: 4 });
    const sessionsSection = screen.getByText('workbench.inbox.sections.sessions').closest('section')!;
    expect(sessionsSection.textContent).toContain('Session 7');
    const botsSection = screen.getByText('workbench.inbox.sections.bots').closest('section')!;
    expect(botsSection.textContent).not.toContain('Session 7');
  });

  it('leaves the Bots section out until a Bot has spoken', () => {
    mountWithBots([row(1, 1), row(2, 2)], {});
    expect(screen.queryByText('workbench.inbox.sections.bots')).toBeNull();
    expect(screen.getByText('workbench.inbox.sections.sessions')).toBeTruthy();
  });

  it('marks the muted category and pictures the mode on the bell', () => {
    mountWithBots([botRow('ses_cso', 1), row(1, 2)], { ses_cso: 1, 'session-1': 3 }, { enabled: true, bots: true, sessions: false });

    expect(screen.getAllByText('workbench.inbox.sections.muted')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'workbench.notifications.open' }).textContent).toContain('workbench.notifications.mode.bots');
  });

  it('swaps in the notification switches from the bell and back', () => {
    mountWithBots([botRow('ses_cso', 1), row(1, 2)], {});

    fireEvent.click(screen.getByRole('button', { name: 'workbench.notifications.open' }));
    expect(screen.getAllByRole('switch').map((node) => node.getAttribute('aria-label'))).toEqual([
      'workbench.notifications.title',
      'workbench.notifications.bots',
      'workbench.notifications.sessions',
    ]);
    expect(rowNames()).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'workbench.notifications.back' }));
    expect(rowNames()).toEqual(['CSO', 'Session 1']);
  });
});
