/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { BotsContext, type BotsState } from '../../../context/BotsContext';
import { UnsavedChangesContext } from '../../../context/unsavedChangesContext';
import type { Bot } from '../../../lib/bots';
import { BotStrip } from './BotStrip';

const api = vi.hoisted(() => ({ removeBot: vi.fn(), listInbox: vi.fn() }));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../../../context/ApiContext', async () => {
  const actual = await vi.importActual<typeof import('../../../context/ApiContext')>('../../../context/ApiContext');
  return { ...actual, useApi: () => api };
});
vi.mock('../../../context/ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  }),
}));

vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
});

const bot = (handle: string, displayName: string, builtin = false): Bot => ({
  handle,
  display_name: displayName,
  avatar: null,
  role: '',
  agent_name: handle,
  session_id: `ses_${handle}`,
  builtin,
});

const bots = [bot('main', '', true), bot('cso', 'Security lead')];

const state = (extra: Partial<BotsState> = {}): BotsState => ({
  enabled: true,
  bots,
  loaded: true,
  listed: true,
  mainBot: bots[0],
  limit: { count: 1, max: 5 },
  atLimit: false,
  statusBySession: {},
  botForSession: (sessionId) => bots.find((each) => each.session_id === sessionId),
  botForAgent: (name) => bots.find((each) => each.agent_name === name),
  botForHandle: (handle) => bots.find((each) => each.handle === handle),
  displayName: (each) => each.display_name || 'Vibey',
  afterWrite: vi.fn().mockResolvedValue(undefined),
  holdOrder: () => () => undefined,
  openProfile: vi.fn(),
  profileRequest: null,
  closeProfile: vi.fn(),
  ...extra,
});

const renderStrip = (value: BotsState) => render(
  <UnsavedChangesContext.Provider value={{ setRegistration: () => {}, authorizeRouteAction: () => null }}>
    <BotsContext.Provider value={value}>
      <MemoryRouter>
        <BotStrip unreadBySession={{}} />
      </MemoryRouter>
    </BotsContext.Provider>
  </UnsavedChangesContext.Provider>,
);

describe('BotStrip', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  // ui/design: agents/bots-redesign/remove-bot: the strip removes a Bot through the
  // same confirmation as the Agents page, which says its conversation and memory stay.
  it('asks before removing a Bot from its menu, and removes it only on confirmation', async () => {
    api.removeBot.mockResolvedValue(undefined);
    const value = state();
    render(
      <UnsavedChangesContext.Provider value={{ setRegistration: () => {}, authorizeRouteAction: () => null }}>
        <BotsContext.Provider value={value}>
          <MemoryRouter>
            <BotStrip unreadBySession={{}} />
          </MemoryRouter>
        </BotsContext.Provider>
      </UnsavedChangesContext.Provider>,
    );

    fireEvent.contextMenu(screen.getByRole('button', { name: 'Security lead' }));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'bots.menu.remove' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('bots.removeConfirm.title:{"name":"Security lead"}')).toBeTruthy();
    expect(api.removeBot).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'bots.menu.remove' }));
    await waitFor(() => expect(api.removeBot).toHaveBeenCalledWith('cso'));
    expect(showToast).toHaveBeenCalledWith('bots.removed:{"name":"Security lead"}', 'success');
    expect(value.afterWrite).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  // docs/plans/bots.md D12; ui/design: sidebar/bots/at-limit. The strip's room goes to the
  // Bots: with every place taken there is no Add at all, not a disabled one.
  it('offers Add while a place is free and drops it once every place is taken', () => {
    const free = state();
    const view = renderStrip(free);
    fireEvent.click(screen.getByRole('button', { name: 'bots.addLabel' }));
    expect(free.openProfile).toHaveBeenCalledWith({ kind: 'create' });

    view.rerender(
      <UnsavedChangesContext.Provider value={{ setRegistration: () => {}, authorizeRouteAction: () => null }}>
        <BotsContext.Provider value={state({ limit: { count: 5, max: 5 }, atLimit: true })}>
          <MemoryRouter>
            <BotStrip unreadBySession={{}} />
          </MemoryRouter>
        </BotsContext.Provider>
      </UnsavedChangesContext.Provider>,
    );
    expect(screen.queryByRole('button', { name: 'bots.addLabel' })).toBeNull();
    expect(screen.queryByText('bots.add')).toBeNull();
    expect(screen.getByRole('button', { name: 'Security lead' })).toBeTruthy();
  });

  it('leaves the Bot in place when the confirmation is cancelled', async () => {
    render(
      <UnsavedChangesContext.Provider value={{ setRegistration: () => {}, authorizeRouteAction: () => null }}>
        <BotsContext.Provider value={state()}>
          <MemoryRouter>
            <BotStrip unreadBySession={{}} />
          </MemoryRouter>
        </BotsContext.Provider>
      </UnsavedChangesContext.Provider>,
    );

    fireEvent.contextMenu(screen.getByRole('button', { name: 'Security lead' }));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'bots.menu.remove' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'common.cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(api.removeBot).not.toHaveBeenCalled();
  });
});
