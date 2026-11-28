// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../i18n/en.json';
import { DESKTOP_OPEN_UPDATES_EVENT } from '../lib/desktopShell';
import { DesktopAccountRow } from './DesktopAccountRow';

const mock = vi.hoisted(() => ({
  present: vi.fn(), connectionStatus: null as string | null, remote: false, phase: 'available', signedIn: true,
  invoke: vi.fn(), getVersion: vi.fn(), localUrl: null as string | null, localAccess: 'off', slug: undefined as string | null | undefined,
}));
vi.mock('../context/ApiContext', () => ({ useApi: () => ({ getVersion: mock.getVersion }) }));
vi.mock('../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => ({ capabilities: { can_manage_instance: true } }) }));
vi.mock('../lib/useIsDesktop', () => ({ useIsDesktop: () => true }));
vi.mock('./useDesktopClient', () => ({
  usePresentDesktopClient: () => mock.present,
  useDesktopClientProjection: () => ({
    account: { status: mock.signedIn ? 'signed_in' : 'signed_out', identity: { id: 'fixture', email: 'max@example.test' } },
    connection: { status: mock.connectionStatus ?? (mock.remote ? 'remote_connected' : 'local'), committed: { kind: mock.remote ? 'remote' : 'local', name: 'Studio', instance_id: 'studio' }, pending: null },
    local: { slug: mock.slug, name: '小麦的 Mac.local', remote_access: mock.localAccess, remote_access_url: mock.localUrl },
    desktop: { version: '3.2.6rc12', update: { phase: mock.phase, latest: '3.2.7' } },
    remote_auxiliary: 'disabled_incomplete',
  }),
}));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
beforeEach(() => {
  mock.connectionStatus = null; mock.remote = false; mock.phase = 'available'; mock.signedIn = true; mock.localUrl = null; mock.localAccess = 'off'; mock.slug = undefined;
  mock.getVersion.mockResolvedValue({ current: '3.2.6rc12', latest: '3.2.7', has_update: true, managed_by: 'desktop' });
  mock.invoke.mockImplementation(async (command: string) => command === 'desktop_update_state'
    ? {
      phase: mock.phase, latest: '3.2.7', channel: 'test', prereleases: true, autoUpdate: false,
      hostActions: ['check', 'prereleases', 'auto_update', 'install'], skippedVersion: null, interactive: false,
    }
    : undefined);
  vi.stubGlobal('__AVIBE_DESKTOP_SHELL__', true);
  vi.stubGlobal('__AVIBE_DESKTOP_VERSION__', '3.2.6rc12');
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke: mock.invoke });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });
function mount() { return render(<I18nextProvider i18n={i18n}><DesktopAccountRow /></I18nextProvider>); }
const installed = () => mock.invoke.mock.calls.filter(([command]) => command === 'desktop_update_install');

// Mount the actual VersionBadge through the production footer, so substituting a second, simpler
// update menu fails. The footer's update is the shared panel (ui/design: update/panel).
it('AUTH-SETUP-178 opens the shared update panel and acts on this computer over the desktop IPC', async () => {
  mount();
  const trigger = await screen.findByTitle('v3.2.6rc12');
  fireEvent.click(trigger);
  const dialog = await screen.findByRole('dialog', { name: en.dashboard.versionAndUpdate });
  // The panel is its own chunk; it arrives just after the dialog opens.
  await waitFor(() => expect(dialog.querySelector('[data-update-panel]')).toBeTruthy());
  expect(within(dialog).getByText(en.update.row.current)).toBeTruthy();
  expect(within(dialog).getByText(en.update.row.offered)).toBeTruthy();
  expect(within(dialog).getByRole('link', { name: /3.2.7/ })).toBeTruthy();
  expect(within(dialog).getByRole('switch', { name: en.update.setting.prereleases })).toBeTruthy();
  fireEvent.click(within(dialog).getByRole('switch', { name: en.update.setting.autoUpdate }));
  await waitFor(() => expect(mock.invoke).toHaveBeenCalledWith('desktop_update_auto_update_set', { enabled: true }));
  expect(within(dialog).getByRole('button', { name: en.dashboard.skipUpdate })).toBeTruthy();
  expect(installed()).toEqual([]);
  fireEvent.click(within(dialog).getByRole('button', { name: en.update.action.update }));
  // Exactly the version the panel shows.
  await waitFor(() => expect(installed()).toEqual([['desktop_update_install', { targetVersion: '3.2.7' }]]));
  expect(mock.present).not.toHaveBeenCalled();
  expect(document.querySelector('[data-update-menu]')).toBeNull();
});

it('AUTH-SETUP-178 routes the native menu to the same panel and preserves Skip', async () => {
  mount();
  await waitFor(() => expect(mock.invoke).toHaveBeenCalledWith('desktop_update_state'));
  const event = new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true });
  await waitFor(() => {
    fireEvent(window, event);
    expect(event.defaultPrevented).toBe(true);
  });
  const dialog = await screen.findByRole('dialog', { name: en.dashboard.versionAndUpdate });
  fireEvent.click(await within(dialog).findByRole('button', { name: en.dashboard.skipUpdate }));
  await waitFor(() => expect(mock.invoke).toHaveBeenCalledWith('desktop_update_skip'));
  expect(mock.present).not.toHaveBeenCalled();
});

it('AUTH-SETUP-178 current version offers no install; account entry stays separate from updates', async () => {
  mock.phase = 'current';
  mount();
  fireEvent.click(await screen.findByTitle('v3.2.6rc12'));
  const dialog = await screen.findByRole('dialog', { name: en.dashboard.versionAndUpdate });
  expect(await within(dialog).findByText(en.update.status.current)).toBeTruthy();
  expect(within(dialog).queryByRole('button', { name: en.update.action.update })).toBeNull();
  expect(document.querySelector('[data-update-menu]')).toBeNull();
  fireEvent.click(within(dialog).getByRole('button', { name: en.common.close }));
  fireEvent.click(document.querySelector('[data-account-trigger]')!);
  expect(mock.present).toHaveBeenCalledExactlyOnceWith('instances');
});

// ui/design: update/instance-and-app. The version beside another computer's name is that instance's, read by
// its own page; the page's API mock has `getVersion` alone, so any install or updater call from here throws.
it("AUTH-SETUP-178 remote Runtime shows the instance's own version and presents the shell's panel without update IPC", async () => {
  mock.remote = true;
  mock.getVersion.mockResolvedValue({ current: '3.2.5', latest: '3.2.7', has_update: true });
  mount();
  // The app is on 3.2.6rc12: that version is no longer shown beside the instance's name.
  fireEvent.click(await screen.findByRole('button', { name: 'Studio is on v3.2.5' }));
  expect(screen.queryByText(/3\.2\.6rc12/)).toBeNull();
  expect(mock.present).toHaveBeenCalledExactlyOnceWith('updates');
  expect(mock.invoke).not.toHaveBeenCalled();
  expect(document.querySelector('[data-update-menu]')).toBeNull();
  mock.present.mockClear();
  const event = new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true });
  fireEvent(window, event);
  expect(event.defaultPrevented).toBe(true);
  expect(mock.present).toHaveBeenCalledExactlyOnceWith('updates');
});

// ui/design: chrome/bottom-row. The owner found the remote version read as plain text, though it
// opens the update panel: on every surface the row is status-left, version-right, and the version
// is the same chip, so a plain-text version or a reversed row fails here.
it('the bottom row reads the same locally and on another computer: status, then the version chip', async () => {
  for (const remote of [false, true]) {
    mock.remote = remote;
    const view = mount();
    const row = document.querySelector('[data-sidebar-status-row]')!;
    const version = remote
      ? await screen.findByRole('button', { name: 'Studio is on v3.2.6rc12' })
      : await screen.findByTitle('v3.2.6rc12');
    expect(version.hasAttribute('data-version-chip')).toBe(true);
    expect(row.lastElementChild!.contains(version)).toBe(true);
    const status = row.querySelector('[data-account-trigger]')!;
    expect(row.firstElementChild!.contains(status)).toBe(true);
    // The status dot leads the computer's name.
    expect(status.firstElementChild!.querySelector('[data-status-indicator]')).toBeTruthy();
    view.unmount();
  }
});

// The previous updater-only test never exercised the account trigger: signed-out
// users received a redundant menu instead of the guarded native account view.
it('AUTH-SETUP-177 signed-out footer directly presents account without opening a menu', async () => {
  mock.signedIn = false;
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  render(<I18nextProvider i18n={i18n}><DesktopAccountRow /></I18nextProvider>);
  fireEvent.click(screen.getByRole('button', { name: /Sign in \/ sign up/ }));
  expect(mock.present).toHaveBeenCalledExactlyOnceWith('account');
  expect(document.querySelector('[data-instance-menu]')).toBeNull();
});

// Owner, 2026-11-28: one activation of the signed-in row is the Instances page, as on the iPhone. A menu
// in between fails here, and so does a row that stops answering while its instance is offline or being
// switched: the page is where the way back to this computer is.
it.each([
  { connection: 'local', remote: false },
  { connection: 'remote_connected', remote: true },
  { connection: 'remote_unreachable', remote: true },
  { connection: 'switching', remote: false },
])('AUTH-SETUP-178 the whole signed-in footer directly opens instances without a popup or caret ($connection)', ({ connection, remote }) => {
  mock.connectionStatus = connection; mock.remote = remote;
  mount();
  const trigger = document.querySelector('[data-account-trigger]')!;
  fireEvent.click(trigger.querySelector('[data-status-indicator]')!);
  expect(mock.present).toHaveBeenCalledExactlyOnceWith('instances');
  expect(screen.queryByRole('menu')).toBeNull();
  // Nothing expands, so the button claims neither a popup nor an expansion state.
  expect(trigger.hasAttribute('aria-haspopup')).toBe(false);
  expect(trigger.hasAttribute('aria-expanded')).toBe(false);
  expect(document.querySelector('[data-account-chevron]')).toBeNull();
  expect(document.querySelector('[data-account-avatar]')).toBeNull();
  expect(trigger.textContent).toBe(remote ? 'Studio' : 'This computer');
});

it.each(['{Enter}', ' '])('AUTH-SETUP-178 keyboard %s on the signed-in footer opens instances at once', async (key) => {
  mount();
  const trigger = document.querySelector<HTMLButtonElement>('[data-account-trigger]')!;
  trigger.focus();
  await userEvent.keyboard(key);
  expect(mock.present).toHaveBeenCalledExactlyOnceWith('instances');
  expect(screen.queryByRole('menu')).toBeNull();
});

// A5 display data must not fall back to an OS hostname or leak the local slug
// into a remote footer. This exercises the product container and shared view.
it.each([
  { remote: false, slug: 'authoritative-local', label: 'authoritative-local' },
  { remote: false, slug: null, label: 'This computer' },
  { remote: false, slug: undefined, label: 'This computer' },
  { remote: true, slug: 'authoritative-local', label: 'Studio' },
])('uses authoritative footer display (remote=$remote, slug=$slug)', ({ remote, slug, label }) => {
  mock.remote = remote; mock.slug = slug;
  mount();
  const trigger = document.querySelector('[data-account-trigger]')!;
  expect(trigger.textContent).toBe(label);
  // The label is the button's name; no hover tip (Max, 2026-11-20).
  expect(trigger.hasAttribute('title')).toBe(false);
});

// Max, 2026-11-22 12:51: the entry reads from the left, the dot then the name, with no instance icon, in
// every state. Its size, wash and the line above it are the e2e's (AUTH-SETUP-178); jsdom lays nothing out.
it.each([
  { remote: false, signedIn: true }, { remote: true, signedIn: true }, { remote: false, signedIn: false },
])('the footer entry starts with the status dot (remote=$remote, signedIn=$signedIn)', ({ remote, signedIn }) => {
  mock.remote = remote; mock.signedIn = signedIn;
  mount();
  const trigger = document.querySelector('[data-account-trigger]')!;
  expect(trigger.firstElementChild!.querySelector('[data-status-indicator]')).toBeTruthy();
  expect(trigger.querySelector('svg:not([data-status-indicator])')).toBeNull();
  expect(trigger.className.split(/\s+/)).not.toContain('justify-center');
  // A plain button, so it carries the keyboard focus ring the shared Button gave it.
  expect(trigger.className.split(/\s+/)).toContain('focus-visible:ring-2');
});

// Max, 2026-11-20: no hover tip in either state, not even for a name too long to show whole.
it.each([
  { signedIn: false, slug: undefined },
  { signedIn: true, slug: 'a-very-long-instance-name-that-the-sidebar-cannot-show-whole' },
])('the footer entry has no hover tip (signedIn=$signedIn)', ({ signedIn, slug }) => {
  mock.signedIn = signedIn; mock.slug = slug;
  mount();
  const trigger = document.querySelector('[data-account-trigger]')!;
  fireEvent.pointerEnter(trigger);
  fireEvent.mouseOver(trigger);
  expect(trigger.hasAttribute('title')).toBe(false);
  expect(trigger.querySelector('[title]')).toBeNull();
});
