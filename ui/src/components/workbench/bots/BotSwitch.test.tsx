/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom';

import { BotsContext, type BotsState } from '../../../context/BotsContext';
import { WorkbenchInboxContext, type InboxState } from '../../../context/WorkbenchInboxContext';
import type { Bot } from '../../../lib/bots';
import { BotConversationTitle } from './BotSwitch';

const viewport = vi.hoisted(() => {
  const state = { isDesktop: false };
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: state.isDesktop,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
  return state;
});

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  }),
  Trans: ({ i18nKey, values }: { i18nKey: string; values?: Record<string, unknown> }) => <>{`${i18nKey}:${JSON.stringify(values)}`}</>,
}));

const bot = (handle: string, name: string, builtin = false): Bot => ({
  handle,
  display_name: name,
  avatar: null,
  role: `${name} role`,
  agent_name: handle,
  session_id: `ses_${handle}`,
  builtin,
  hide_details: true,
});
const team = [bot('main', 'Vibey', true), bot('cso', 'Security lead'), bot('pm', 'PM')];
const openProfile = vi.fn();

const state: BotsState = {
  enabled: true,
  bots: team,
  loaded: true,
  listed: true,
  mainBot: team[0],
  limit: { count: 2, max: 5 },
  atLimit: false,
  statusBySession: { ses_pm: 'running' },
  botForSession: (id) => team.find((each) => each.session_id === id),
  botForAgent: () => undefined,
  botForHandle: (handle) => team.find((each) => each.handle === handle),
  displayName: (each) => each.display_name,
  afterWrite: async () => undefined,
  holdOrder: () => () => undefined,
  setHideDetails: async () => true,
  openProfile,
  profileRequest: null,
  closeProfile: () => undefined,
};

const inbox = {
  unreadBySession: { ses_main: 1, ses_cso: 4, ses_pm: 2, ses_other: 9 },
  inboxSessions: [
    { session_id: 'ses_pm', preview_text: '**The backlog** is ranked:\n\n- `auth` first\n- docs next', last_activity_at: new Date().toISOString() },
  ],
} as unknown as InboxState;

const ChatProbe = () => {
  const { sessionId } = useParams();
  const current = team.find((each) => each.session_id === sessionId);
  return (
    <div>
      <div data-testid="route">{sessionId}</div>
      {current && <BotConversationTitle bot={current} />}
    </div>
  );
};

function renderTitle(sessionId = 'ses_cso', value: BotsState = state) {
  return render(
    <BotsContext.Provider value={value}>
      <WorkbenchInboxContext.Provider value={inbox}>
        <MemoryRouter initialEntries={[`/chat/${sessionId}`]}>
          <Routes>
            <Route path="/chat/:sessionId" element={<ChatProbe />} />
          </Routes>
        </MemoryRouter>
      </WorkbenchInboxContext.Provider>
    </BotsContext.Provider>,
  );
}

beforeEach(() => {
  viewport.isDesktop = false;
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// ui/design: mobile/bot-conversation/header and /switch: on a phone a Bot
// conversation's name is the way to the other Bots.
describe('BotConversationTitle', () => {
  it('counts the other Bots’ unread replies on the switch, not this one’s or an ordinary Session’s', () => {
    renderTitle();
    const trigger = screen.getByRole('button', { name: 'bots.switchBot:{"name":"Security lead"}' });
    expect(within(trigger).getByLabelText('bots.othersUnread:{"count":3}').textContent).toBe('3');
  });

  it('opens every Bot, and picking another opens its conversation', () => {
    renderTitle();
    fireEvent.click(screen.getByRole('button', { name: 'bots.switchBot:{"name":"Security lead"}' }));

    const sheet = screen.getByRole('dialog');
    const rows = within(sheet).getAllByRole('listitem').map((row) => row.textContent);
    expect(rows[0]).toContain('Vibey');
    expect(rows[0]).toContain('bots.mainBadge');
    // Without a message yet, a row shows the role; the main Bot's is its UI copy, never
    // the Agent-facing description it stores.
    expect(rows[0]).toContain('bots.mainRole');
    expect(rows[0]).not.toContain('Vibey role');
    // A Markdown reply reads as one line of plain text, the same excerpt the strip's card shows.
    expect(rows[2]).toContain('bots.runningPreview:{"preview":"The backlog is ranked: auth first docs next"}');
    expect(rows[1]).toContain('Security lead role');
    expect(within(sheet).getByRole('button', { current: 'page' }).textContent).toContain('Security lead');

    fireEvent.click(within(sheet).getByRole('button', { name: /PM/ }));
    expect(screen.getByTestId('route').textContent).toBe('ses_pm');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('adds a Bot from the sheet through the shared add dialog', () => {
    renderTitle();
    fireEvent.click(screen.getByRole('button', { name: 'bots.switchBot:{"name":"Security lead"}' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'bots.add' }));

    expect(openProfile).toHaveBeenCalledWith({ kind: 'create' });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  // docs/plans/bots.md D12: as on the sidebar strip, a full team leaves no Add in the sheet.
  it('has no Add once every place for a custom Bot is taken', () => {
    renderTitle('ses_cso', { ...state, limit: { count: 5, max: 5 }, atLimit: true });
    fireEvent.click(screen.getByRole('button', { name: 'bots.switchBot:{"name":"Security lead"}' }));
    const sheet = screen.getByRole('dialog');
    expect(within(sheet).getAllByRole('listitem')).toHaveLength(3);
    expect(within(sheet).queryByRole('button', { name: 'bots.add' })).toBeNull();
  });

  // A desktop has the sidebar's strip for switching, so the name stays a title.
  it('stays a plain title on a desktop', () => {
    viewport.isDesktop = true;
    renderTitle();
    expect(screen.getByText('Security lead')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
});
