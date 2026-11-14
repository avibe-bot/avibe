/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  getHomePreferences: vi.fn(),
  updateHomePreferences: vi.fn(),
  getConfig: vi.fn(),
  remoteAccessStatus: vi.fn(),
}));
const push = vi.hoisted(() => ({
  state: 'unavailable' as 'on' | 'off' | 'unavailable',
  enable: vi.fn(),
  read: vi.fn(),
}));
const computerUse = vi.hoisted(() => ({ read: vi.fn() }));
const native = vi.hoisted(() => ({ shell: false }));
const authorization = vi.hoisted(() => ({
  capabilities: { can_read_instance: true, can_use_show_pages: true, can_use_vault_secrets: true, can_use_skills: true },
}));
const viewport = vi.hoisted(() => ({ desktop: true }));

vi.mock('../../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/ApiContext')>()),
  useApi: () => api,
}));
// This device's push: the browser's own state is the webPush module's business; here only what it reports.
vi.mock('../../lib/webPush', () => ({
  readWebPushDeviceState: (...args: unknown[]) => push.read(...args),
  enableWebPush: (...args: unknown[]) => push.enable(...args),
}));
vi.mock('../../lib/native', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/native')>()),
  isNativeShell: () => native.shell,
}));
vi.mock('../workbench/computerUseStatus', () => ({ readComputerUseStatus: () => computerUse.read() }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => authorization }));
vi.mock('../../lib/useIsDesktop', () => ({ useIsDesktop: () => viewport.desktop }));

import { BotsContext, type BotsState } from '../../context/BotsContext';
import en from '../../i18n/en.json';
import type { Bot } from '../../lib/bots';
import { localDate, type HomePreferences } from '../../lib/homePreferences';
import { resetCoalescedWrites } from '../../lib/useCoalescedWrite';
import { HomeGuide } from './HomeGuide';
import { dayNumber, tipsForDay } from './tips';
import { useHomePreferences } from './useHomePreferences';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const bot = (handle: string, builtin = false): Bot => ({
  handle, display_name: handle, avatar: null, role: '', agent_name: handle, session_id: `ses_${handle}`,
  builtin, hide_details: true,
});
const openProfile = vi.fn();
const botsState = (bots: Bot[]) => ({
  enabled: true, bots, mainBot: bots[0] ?? null, openProfile, displayName: (each: Bot) => each.display_name,
}) as unknown as BotsState;

const today = localDate(new Date());
const prefs = (changes: Partial<HomePreferences> = {}): HomePreferences => ({
  setup_later_on: null, setup_not_needed: [], tip_hidden_on: null, tips_enabled: true, ...changes,
});

const Where = () => <div data-testid="where">{useLocation().pathname}</div>;
const onAsk = vi.fn();

function renderGuide({ owner = true, noModel = false, bots = [bot('main', true)] } = {}) {
  return render(
    <I18nextProvider i18n={i18n}>
      <BotsContext.Provider value={botsState(bots)}>
        <MemoryRouter initialEntries={['/']}>
          <Routes><Route path="*" element={<Where />} /></Routes>
          <HomeGuide owner={owner} noModel={noModel} name="Vibey" onAsk={onAsk} />
        </MemoryRouter>
      </BotsContext.Provider>
    </I18nextProvider>,
  );
}

const card = () => screen.queryByRole('heading', { name: en.workbench.home.setup.title })?.closest('section') ?? null;
const items = () => {
  const section = card();
  if (!section) return [];
  const titles = Object.values(en.workbench.home.setup)
    .filter((entry): entry is { title: string } => typeof entry === 'object' && entry !== null && 'title' in entry)
    .map((entry) => entry.title.replace('{{name}}', 'Vibey'));
  return titles.filter((title) => within(section).queryByText(title));
};
const where = () => screen.getByTestId('where').textContent;
const tipSection = () => screen.queryByRole('region', { name: en.workbench.home.tips.label });

beforeEach(() => {
  viewport.desktop = true;
  push.state = 'unavailable';
  push.enable.mockReset();
  push.read.mockReset().mockImplementation(async () => push.state);
  native.shell = false;
  computerUse.read.mockResolvedValue({ kind: 'unsupported' });
  // The server's record: a save merges into it (the not-needed list only grows), a read returns it.
  let stored = prefs();
  api.getHomePreferences.mockImplementation(async () => stored);
  api.updateHomePreferences.mockImplementation(async (changes: Partial<HomePreferences>) => {
    stored = {
      ...stored,
      ...changes,
      setup_not_needed: [...new Set([...stored.setup_not_needed, ...(changes.setup_not_needed ?? [])])],
    };
    return stored;
  });
  api.getConfig.mockResolvedValue({ platforms: { enabled: ['slack'] } });
  api.remoteAccessStatus.mockResolvedValue({ ok: true, paired: true });
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  // The account's writer is module state; each case starts with none in flight.
  resetCoalescedWrites();
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
});

describe('Getting started reads what the UI already observes (ui/design: home/home/a-new-*, b-partial-*)', () => {
  it('lists each missing fact in order, two at a time, and opens where each is fixed', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    push.state = 'off';
    computerUse.read.mockResolvedValue({ kind: 'status', status: { status: 'needs_permission', reason: 'accessibility' } });
    api.getConfig.mockResolvedValue({ platforms: { enabled: [] } });
    api.remoteAccessStatus.mockResolvedValue({ ok: true, paired: false });
    const user = userEvent.setup();
    renderGuide();
    await waitFor(() => expect(items()).toEqual(['Turn on notifications', 'Allow Computer Use']));
    await user.click(screen.getByRole('button', { name: '2 more' }));
    expect(items()).toEqual(['Turn on notifications', 'Allow Computer Use', 'Connect a chat app', 'Continue on your phone']);
    for (const [action, path] of [['Grant', '/settings/computer-use'], ['Connect', '/settings/platforms'], ['Pair', '/settings/remote-access']]) {
      await user.click(screen.getByRole('button', { name: action }));
      expect(where()).toBe(path);
    }
    // Notifications turn on in place.
    await user.click(screen.getByRole('button', { name: 'Turn on' }));
    expect(push.enable).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a Windows host', 'windows', 'Turn on Computer Use (Preview)', 'Turn on'],
    ['a macOS host', 'macos', 'Allow Computer Use', 'Grant'],
  ])('names Computer Use setup for %s, as the server reports it', async (_case, host, title, action) => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    computerUse.read.mockResolvedValue({ kind: 'status', host, status: { status: 'off', reason: 'never_enabled' } });
    const user = userEvent.setup();
    renderGuide();
    await waitFor(() => expect(items()).toEqual([title]));
    await user.click(screen.getByRole('button', { name: action }));
    expect(where()).toBe('/settings/computer-use');
  });

  it('leads with the model alone while there is none, and its one solid button opens Settings · Models', async () => {
    push.state = 'off';
    api.getConfig.mockResolvedValue({ platforms: { enabled: [] } });
    const user = userEvent.setup();
    renderGuide({ noModel: true });
    await waitFor(() => expect(items()).toEqual(['Give Vibey a model']));
    // The model cannot be waved off; only "Later" puts it off.
    expect(screen.queryByRole('button', { name: en.workbench.home.setup.notNeeded })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Add one' }));
    expect(where()).toBe('/settings/models');
  });

  it.each([
    ['nothing is missing', () => {}],
    ['every read fails', () => {
      api.getConfig.mockRejectedValue(new Error('down'));
      api.remoteAccessStatus.mockRejectedValue(new Error('down'));
      computerUse.read.mockResolvedValue(null);
    }],
  ])('shows no card when %s', async (_case, arrange) => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    arrange();
    renderGuide();
    await waitFor(() => expect(api.getHomePreferences).toHaveBeenCalled());
    await waitFor(() => expect(tipSection()).not.toBeNull());
    expect(card()).toBeNull();
  });

  it('leaves out what a phone, a browser tab, or a member does not fix', async () => {
    push.state = 'off';
    computerUse.read.mockResolvedValue({ kind: 'status', status: { status: 'off', reason: null } });
    api.getConfig.mockResolvedValue({ platforms: { enabled: [] } });
    api.remoteAccessStatus.mockResolvedValue({ ok: true, paired: false });
    viewport.desktop = false;
    const phone = renderGuide();
    // Not the desktop app: no Computer Use; a phone: no "continue on your phone"; one item shown.
    await waitFor(() => expect(items()).toEqual(['Turn on notifications']));
    expect(screen.getByRole('button', { name: '1 more' })).toBeTruthy();
    phone.unmount();
    viewport.desktop = true;
    renderGuide({ owner: false, bots: [] });
    await waitFor(() => expect(items()).toEqual(['Turn on notifications']));
    expect(screen.queryByRole('button', { name: /more$/ })).toBeNull();
    expect(api.getConfig).toHaveBeenCalledTimes(1);
  });

  it('does not read or offer browser Web Push in the native shell', async () => {
    native.shell = true;
    push.state = 'off';
    renderGuide();

    await waitFor(() => expect(api.getHomePreferences).toHaveBeenCalled());
    expect(push.read).not.toHaveBeenCalled();
    expect(items()).not.toContain('Turn on notifications');
  });
});

describe('Getting started remembers the account’s answers', () => {
  it('puts the card off until tomorrow with "Later", and drops an item for good with "Not needed"', async () => {
    push.state = 'off';
    api.getConfig.mockResolvedValue({ platforms: { enabled: [] } });
    const user = userEvent.setup();
    renderGuide();
    await waitFor(() => expect(items()).toEqual(['Turn on notifications', 'Connect a chat app']));
    await user.click(within(card()!).getAllByRole('button', { name: en.workbench.home.setup.notNeeded })[0]);
    expect(api.updateHomePreferences).toHaveBeenLastCalledWith({ setup_not_needed: ['notifications'] });
    expect(items()).toEqual(['Connect a chat app']);
    await user.click(screen.getByRole('button', { name: en.workbench.home.setup.later }));
    expect(api.updateHomePreferences).toHaveBeenLastCalledWith({ setup_later_on: today });
    expect(card()).toBeNull();
  });

  it('saves one change at a time, so the newest choice is the last one the server commits', async () => {
    push.state = 'off';
    api.getConfig.mockResolvedValue({ platforms: { enabled: [] } });
    let release: () => void = () => {};
    const save = api.updateHomePreferences.getMockImplementation()!;
    api.updateHomePreferences.mockImplementationOnce((changes: Partial<HomePreferences>) =>
      new Promise((resolve) => { release = () => resolve(save(changes)); }));
    const user = userEvent.setup();
    renderGuide();
    await waitFor(() => expect(items()).toEqual(['Turn on notifications', 'Connect a chat app']));
    await user.click(within(card()!).getAllByRole('button', { name: en.workbench.home.setup.notNeeded })[0]);
    await user.click(within(card()!).getAllByRole('button', { name: en.workbench.home.setup.notNeeded })[0]);
    // Both are gone at once; the second save waits behind the first.
    expect(card()).toBeNull();
    expect(api.updateHomePreferences.mock.calls).toEqual([[{ setup_not_needed: ['notifications'] }]]);
    release();
    await waitFor(() => expect(api.updateHomePreferences).toHaveBeenCalledTimes(2));
    expect(api.updateHomePreferences.mock.calls[1]).toEqual([{ setup_not_needed: ['chat_apps'] }]);
  });

  it.each([
    ['put off today', prefs({ setup_later_on: today }), []],
    ['put off on an earlier day', prefs({ setup_later_on: '2000-01-01' }), ['Turn on notifications', 'Connect a chat app']],
    ['with an item not needed', prefs({ setup_not_needed: ['chat_apps'] }), ['Turn on notifications']],
  ])('reads the record as stored: %s', async (_case, stored, expected) => {
    push.state = 'off';
    api.getConfig.mockResolvedValue({ platforms: { enabled: [] } });
    api.getHomePreferences.mockResolvedValue(stored);
    renderGuide();
    await waitFor(() => expect(api.getHomePreferences).toHaveBeenCalled());
    await waitFor(() => expect(items()).toEqual(expected));
  });

  it('shows nothing before the record is read, so a card put off today does not flash up', async () => {
    push.state = 'off';
    api.getHomePreferences.mockReturnValue(new Promise(() => {}));
    renderGuide();
    await waitFor(() => expect(api.getConfig).toHaveBeenCalled());
    expect(card()).toBeNull();
    expect(tipSection()).toBeNull();
  });
});

describe('the tip of the day (ui/design: home/home/c-ready-*)', () => {
  it('puts the capabilities not used yet first, and the day picks among them', () => {
    const all = ['bots', 'showPages', 'tasks', 'vault'] as const;
    const used = new Set(['bots', 'vault'] as const);
    expect(tipsForDay(all, used, 0)).toEqual({ order: ['showPages', 'tasks', 'bots', 'vault'], start: 0 });
    expect(tipsForDay(all, used, 1).start).toBe(1);
    expect(tipsForDay(all, used, 2).start).toBe(0);
    // Every one used: the day picks among all of them.
    expect(tipsForDay(all, new Set(all), 3)).toEqual({ order: [...all], start: 3 });
    expect(tipsForDay([], new Set(), 5)).toEqual({ order: [], start: 0 });
    // The reader's own calendar day.
    expect(dayNumber(new Date(2026, 10, 4, 0, 1))).toBe(dayNumber(new Date(2026, 10, 4, 23, 59)));
    expect(dayNumber(new Date(2026, 10, 5, 0, 1)) - dayNumber(new Date(2026, 10, 4, 23, 59))).toBe(1);
  });

  it('shows today’s tip, walks the others, and tries one by putting its ask in the composer', async () => {
    const user = userEvent.setup();
    // A custom Bot exists and a chat app is connected: both count as used.
    renderGuide({ bots: [bot('main', true), bot('pm')] });
    await waitFor(() => expect(tipSection()).not.toBeNull());
    const available = ['showPages', 'tasks', 'vault', 'skills', 'bots', 'chatApps'] as const;
    const { order, start } = tipsForDay(['bots', 'showPages', 'tasks', 'vault', 'skills', 'chatApps'], new Set(['bots', 'chatApps']), dayNumber(new Date()));
    expect(order).toEqual([...available]);
    const titleOf = (id: string) => en.workbench.home.tips[id as keyof typeof en.workbench.home.tips] as { title: string };
    expect(tipSection()!.textContent).toContain(titleOf(order[start]).title);
    // Walk to Tasks and Watches, whose "Try" fills the composer and never sends.
    const steps = (order.indexOf('tasks') - start + order.length) % order.length;
    for (let i = 0; i < steps; i += 1) await user.click(screen.getByRole('button', { name: en.workbench.home.tips.next }));
    expect(tipSection()!.textContent).toContain(en.workbench.home.tips.tasks.title);
    await user.click(screen.getByRole('button', { name: en.workbench.home.tips.try }));
    expect(onAsk).toHaveBeenCalledWith(en.workbench.home.tips.tasks.ask);
  });

  it('hides for today with ×, and stays away while there is no model or tips are off', async () => {
    const user = userEvent.setup();
    const view = renderGuide();
    await waitFor(() => expect(tipSection()).not.toBeNull());
    await user.click(screen.getByRole('button', { name: en.workbench.home.tips.hide }));
    expect(api.updateHomePreferences).toHaveBeenLastCalledWith({ tip_hidden_on: today });
    expect(tipSection()).toBeNull();
    view.unmount();

    for (const [noModel, stored] of [[true, prefs()], [false, prefs({ tips_enabled: false })], [false, prefs({ tip_hidden_on: today })]] as const) {
      api.getHomePreferences.mockResolvedValue(stored);
      const each = renderGuide({ noModel });
      await waitFor(() => expect(api.getHomePreferences).toHaveBeenCalled());
      await waitFor(() => expect(api.getConfig).toHaveBeenCalled());
      expect(tipSection()).toBeNull();
      each.unmount();
    }
  });

  it('follows a change made elsewhere once it is saved, even after that surface has closed', async () => {
    // Settings · General's switch, standing in for the page: another reader of the same record.
    const Switch = () => {
      const { preferences, update } = useHomePreferences();
      return <button type="button" onClick={() => update({ tips_enabled: !preferences.tips_enabled })}>tips-switch</button>;
    };
    let release: () => void = () => {};
    const save = api.updateHomePreferences.getMockImplementation()!;
    api.updateHomePreferences.mockImplementationOnce((changes: Partial<HomePreferences>) =>
      new Promise((resolve) => { release = () => resolve(save(changes)); }));
    const user = userEvent.setup();
    renderGuide();
    await waitFor(() => expect(tipSection()).not.toBeNull());
    const settings = render(<Switch />);
    await user.click(screen.getByText('tips-switch'));
    // Settings closes before the save lands; the home behind it still shows the tip.
    settings.unmount();
    expect(tipSection()).not.toBeNull();
    release();
    await waitFor(() => expect(tipSection()).toBeNull());
  });

  it('offers a member only the tips they can act on', async () => {
    authorization.capabilities.can_use_vault_secrets = false;
    try {
      const user = userEvent.setup();
      renderGuide({ owner: false, bots: [] });
      await waitFor(() => expect(tipSection()).not.toBeNull());
      const seen = new Set<string>();
      for (let i = 0; i < 4; i += 1) {
        seen.add(tipSection()!.querySelector('.font-semibold')!.textContent!);
        await user.click(screen.getByRole('button', { name: en.workbench.home.tips.next }));
      }
      expect([...seen].sort()).toEqual([
        en.workbench.home.tips.showPages.title,
        en.workbench.home.tips.skills.title,
        en.workbench.home.tips.tasks.title,
      ].sort());
    } finally {
      authorization.capabilities.can_use_vault_secrets = true;
    }
  });
});
