// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../i18n/en.json';
import { DESKTOP_OPEN_UPDATES_EVENT } from '../lib/desktopShell';
import { DesktopAccountRow } from './DesktopAccountRow';

const mock = vi.hoisted(() => ({
  present: vi.fn(), remote: false, phase: 'available',
  invoke: vi.fn(), getVersion: vi.fn(),
}));
vi.mock('../context/ApiContext', () => ({ useApi: () => ({ getVersion: mock.getVersion }) }));
vi.mock('../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => ({ capabilities: { can_manage_instance: true } }) }));
vi.mock('../lib/useIsDesktop', () => ({ useIsDesktop: () => true }));
vi.mock('./useDesktopClient', () => ({
  usePresentDesktopClient: () => mock.present,
  useDesktopClientProjection: () => ({
    account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' } },
    connection: { status: mock.remote ? 'remote_connected' : 'local', committed: { kind: mock.remote ? 'remote' : 'local', name: 'Studio', instance_id: 'studio' }, pending: null },
    local: { name: '小麦的 Mac', remote_access: 'off' },
    desktop: { version: '3.2.6rc12', update: { phase: mock.phase, latest: '3.2.7' } },
    remote_auxiliary: 'disabled_incomplete',
  }),
}));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
beforeEach(() => {
  mock.remote = false; mock.phase = 'available';
  mock.getVersion.mockResolvedValue({ current: '3.2.6rc12', latest: '3.2.7', has_update: true, managed_by: 'desktop' });
  mock.invoke.mockImplementation(async (command: string) => command === 'desktop_update_state'
    ? { phase: mock.phase, latest: '3.2.7', channel: 'test', prereleases: true, skippedVersion: null, interactive: false }
    : undefined);
  vi.stubGlobal('__AVIBE_DESKTOP_SHELL__', true);
  vi.stubGlobal('__AVIBE_DESKTOP_VERSION__', '3.2.6rc12');
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke: mock.invoke });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });
function mount() { return render(<I18nextProvider i18n={i18n}><DesktopAccountRow /></I18nextProvider>); }

// The former test only asserted the new compact menu. Mount the actual rc12
// VersionBadge through the production footer so substituting that menu fails.
it('AUTH-SETUP-178 opens the original version dialog and retains its local updater actions', async () => {
  mount();
  const trigger = await screen.findByTitle('v3.2.6rc12');
  fireEvent.click(trigger);
  const dialog = await screen.findByRole('dialog', { name: 'Version & Update' });
  expect(within(dialog).getByText('Current Version')).toBeTruthy();
  expect(within(dialog).getByText('Latest Version')).toBeTruthy();
  expect(within(dialog).getByRole('link', { name: /3.2.7/ })).toBeTruthy();
  expect(within(dialog).getByText('Get pre-release updates')).toBeTruthy();
  expect(within(dialog).getByRole('button', { name: 'Skip this version' })).toBeTruthy();
  expect(mock.invoke).not.toHaveBeenCalledWith('desktop_update_install');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Upgrade Now' }));
  await waitFor(() => expect(mock.invoke).toHaveBeenCalledWith('desktop_update_install'));
  expect(mock.present).not.toHaveBeenCalled();
  expect(document.querySelector('[data-update-menu]')).toBeNull();
});

it('AUTH-SETUP-178 routes the native menu to the same original dialog and preserves Skip', async () => {
  mount();
  await waitFor(() => expect(mock.invoke).toHaveBeenCalledWith('desktop_update_state'));
  const event = new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true });
  fireEvent(window, event);
  expect(event.defaultPrevented).toBe(true);
  const dialog = await screen.findByRole('dialog', { name: 'Version & Update' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Skip this version' }));
  await waitFor(() => expect(mock.invoke).toHaveBeenCalledWith('desktop_update_skip'));
  expect(mock.present).not.toHaveBeenCalled();
});

it('AUTH-SETUP-178 current version never opens the compact menu; account menu has no updater action', async () => {
  mock.phase = 'current';
  mount();
  fireEvent.click(await screen.findByTitle('v3.2.6rc12'));
  const dialog = await screen.findByRole('dialog', { name: 'Version & Update' });
  expect(within(dialog).queryByRole('button', { name: 'Upgrade Now' })).toBeNull();
  expect(document.querySelector('[data-update-menu]')).toBeNull();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
  fireEvent.click(document.querySelector('[data-account-trigger]')!);
  const menu = document.querySelector('[data-instance-menu]')!;
  expect(menu.textContent).not.toMatch(/update|upgrade/i);
});

it('AUTH-SETUP-178 remote Runtime presents the trusted native decision without local or remote update IPC', async () => {
  mock.remote = true;
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Local Desktop version 3.2.6rc12' }));
  expect(mock.present).toHaveBeenCalledExactlyOnceWith('updates');
  expect(mock.invoke).not.toHaveBeenCalled();
  expect(mock.getVersion).not.toHaveBeenCalled();
  expect(document.querySelector('[data-update-menu]')).toBeNull();
  mock.present.mockClear();
  const event = new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true });
  fireEvent(window, event);
  expect(event.defaultPrevented).toBe(true);
  expect(mock.present).toHaveBeenCalledExactlyOnceWith('updates');
});
