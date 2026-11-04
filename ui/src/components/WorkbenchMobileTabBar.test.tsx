/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom';
import { Inbox } from 'lucide-react';

import { BotsContext, type BotsState } from '../context/BotsContext';
import { WorkbenchInboxContext, type InboxState } from '../context/WorkbenchInboxContext';
import type { Bot } from '../lib/bots';
import { WorkbenchMobileTabBar } from './WorkbenchMobileTabBar';

const api = vi.hoisted(() => ({ sendSessionMessage: vi.fn() }));
// The hold's own path is useHoldToTalk's test; here the bar hands it what to send and where a tap goes.
const hold = vi.hoisted(() => ({ options: null as null | { send: (text: string, metadata: object) => Promise<boolean>; onTap?: () => void } }));
vi.mock('../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../context/ApiContext')>()),
  useApi: () => api,
}));
vi.mock('./voice/useHoldToTalk', () => ({
  useHoldToTalk: (options: NonNullable<typeof hold.options>) => {
    hold.options = options;
    return {
      phase: 'idle', interruptReason: null, text: '', stash: '', seconds: 0, progress: 0, outcome: null,
      // The hold has answered every pointer press here: it spends the click that follows.
      available: true, holdable: true, canRetry: false,
      bind: { onClick: (event: { detail: number; preventDefault: () => void }) => { if (event.detail !== 0) event.preventDefault(); } },
      sendInterrupted: () => {}, retry: () => {}, discard: () => {}, dismiss: () => {},
    };
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  }),
}));

const main: Bot = {
  handle: 'main',
  display_name: 'Vibey',
  avatar: { kind: 'mascot' },
  role: '',
  agent_name: 'vibey-main',
  session_id: 'ses_main',
  builtin: true,
  hide_details: true,
};

const bots = (overrides: Partial<BotsState>): BotsState => ({
  enabled: true,
  bots: [main],
  loaded: true,
  listed: true,
  mainBot: main,
  statusBySession: {},
  botForSession: () => undefined,
  botForAgent: () => undefined,
  botForHandle: () => undefined,
  displayName: (bot) => bot.display_name,
  afterWrite: async () => undefined,
  holdOrder: () => () => undefined,
  setHideDetails: async () => true,
  openProfile: () => undefined,
  profileRequest: null,
  closeProfile: () => undefined,
  ...overrides,
});

const inbox = (unreadBySession: Record<string, number>) => ({ unreadBySession }) as unknown as InboxState;

const ChatProbe = () => <div data-testid="chat">{useParams().sessionId}</div>;

function renderBar(state: BotsState, unread: Record<string, number> = {}, onNewSession?: () => void) {
  return render(
    <BotsContext.Provider value={state}>
      <WorkbenchInboxContext.Provider value={inbox(unread)}>
        <MemoryRouter initialEntries={['/projects']}>
          <Routes>
            <Route path="/projects" element={<WorkbenchMobileTabBar items={[{ to: '/inbox', label: 'nav.inbox', icon: Inbox }]} onNewSession={onNewSession} />} />
            <Route path="/chat/:sessionId" element={<ChatProbe />} />
          </Routes>
        </MemoryRouter>
      </WorkbenchInboxContext.Provider>
    </BotsContext.Provider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  hold.options = null;
});

// ui/design: mobile/workbench-tab/team: for the Owner the middle of the bar is the main
// Bot, with its unread count and running dot; a tap opens its conversation.
describe('WorkbenchMobileTabBar', () => {
  it('puts the main Bot in the middle for the Owner, and a tap opens its conversation', () => {
    const onNewSession = vi.fn();
    renderBar(bots({ statusBySession: { ses_main: 'running' } }), { ses_main: 3 }, onNewSession);

    const center = screen.getByRole('button', { name: 'appShell.openMainBot:{"name":"Vibey"}' });
    expect(center.textContent).toContain('3');
    expect(center.textContent).toContain('Vibey');
    expect(center.querySelector('.animate-ping')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'appShell.newSession' })).toBeNull();

    // A pointer's tap reaches the bar through the hold (inside the release); a keyboard's is a click.
    act(() => hold.options?.onTap?.());
    expect(screen.getByTestId('chat').textContent).toBe('ses_main');
    expect(onNewSession).not.toHaveBeenCalled();
  });

  it('keeps the main Bot there before the Bot list answers, under its default name', () => {
    renderBar(bots({ bots: [], loaded: false, listed: false, mainBot: null }), {}, vi.fn());

    fireEvent.click(screen.getByRole('button', { name: 'appShell.openMainBot:{"name":"bots.mainDefaultName"}' }), { detail: 0 });
    expect(screen.getByTestId('chat').textContent).toBe('ses_main');
  });

  // ui/design: mobile/main-bot-voice/h-sent: what a hold says goes to the main Bot's
  // conversation, marked as spoken, and the reader stays on the page.
  it('sends a held take to the main Bot as a voice message, without leaving the page', async () => {
    api.sendSessionMessage.mockResolvedValue({ id: 'msg-1' });
    renderBar(bots({}), {}, vi.fn());

    await expect(hold.options!.send('Check the release', { input: 'voice' })).resolves.toBe(true);
    expect(api.sendSessionMessage).toHaveBeenCalledWith('ses_main', { text: 'Check the release', metadata: { input: 'voice' } });
    expect(screen.queryByTestId('chat')).toBeNull();
  });

  it('ignores a pointer click on the center, which the hold already answered', () => {
    renderBar(bots({}), {}, vi.fn());
    fireEvent.click(screen.getByRole('button', { name: /appShell\.openMainBot/ }), { detail: 1 });
    expect(screen.queryByTestId('chat')).toBeNull();
  });

  // ui/design: mobile/workbench-tab/member: a reader without Bots keeps the new-session ＋.
  it('keeps the new-session ＋ for a reader without Bots', () => {
    const onNewSession = vi.fn();
    renderBar(bots({ enabled: false, bots: [], mainBot: null }), {}, onNewSession);

    expect(screen.queryByRole('button', { name: /appShell\.openMainBot/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'appShell.newSession' }));
    expect(onNewSession).toHaveBeenCalledTimes(1);
  });
});
