// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../i18n/en.json';
import { DESKTOP_OPEN_UPDATES_EVENT } from '../lib/desktopShell';
import { DesktopAccountRow } from './DesktopAccountRow';

const present = vi.hoisted(() => vi.fn());
const projection = vi.hoisted(() => ({
  committed: { kind: 'remote', name: 'Studio', instance_id: 'studio' } as Record<string, unknown>,
  hostActions: ['check', 'prereleases', 'auto_update'] as string[] | undefined,
}));
vi.mock('./useDesktopClient', () => ({
  usePresentDesktopClient: () => present,
  useDesktopClientProjection: () => ({
    account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' } },
    connection: { status: projection.committed.kind === 'local' ? 'local' : 'remote_connected', committed: projection.committed, pending: null },
    local: { name: '小麦的 Mac', remote_access: 'off' },
    desktop: {
      version: '3.2.6rc9',
      update: { phase: 'available', latest: '3.2.7', autoUpdate: false, prereleases: false, hostActions: projection.hostActions },
    },
    remote_auxiliary: 'disabled_incomplete',
  }),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

async function renderRow() {
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  render(<I18nextProvider i18n={i18n}><DesktopAccountRow /></I18nextProvider>);
}

it('AUTH-SETUP-178 another computer\'s page opens this computer\'s update in the trusted view', async () => {
  projection.committed = { kind: 'remote', name: 'Studio', instance_id: 'studio' };
  await renderRow();
  const event = new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true });
  act(() => { window.dispatchEvent(event); });
  expect(event.defaultPrevented).toBe(true);
  expect(window.__AVIBE_DESKTOP_UPDATES_HANDLED__).toBe(true);
  expect(present).toHaveBeenCalledExactlyOnceWith('updates');
  expect(screen.queryByText(en.update.status.available)).toBeNull();
});

it('AUTH-SETUP-178 a page this computer serves shows the same panel and acts over the desktop IPC', async () => {
  projection.committed = { kind: 'local', name: '小麦的 Mac', instance_id: null };
  const invoke = vi.fn(async () => undefined);
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
  Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
  await renderRow();
  act(() => { window.dispatchEvent(new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true })); });
  expect(await screen.findByText(en.update.status.available)).toBeTruthy();
  expect(screen.getByText('3.2.6rc9')).toBeTruthy();
  fireEvent.click(screen.getByRole('switch', { name: en.update.setting.autoUpdate }));
  expect(invoke).toHaveBeenCalledWith('desktop_update_auto_update_set', { enabled: true });
  fireEvent.click(screen.getByRole('button', { name: en.update.action.update }));
  // Exactly the version the panel shows.
  expect(invoke).toHaveBeenCalledWith('desktop_update_install', { targetVersion: '3.2.7' });
  expect(present).not.toHaveBeenCalled();
});

it('AUTH-SETUP-178 a shell that predates hostActions keeps its pre-release switch in the footer', async () => {
  projection.committed = { kind: 'local', name: '小麦的 Mac', instance_id: null };
  projection.hostActions = undefined;
  const invoke = vi.fn(async () => undefined);
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
  Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
  await renderRow();
  act(() => { window.dispatchEvent(new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true })); });
  fireEvent.click(await screen.findByRole('switch', { name: en.update.setting.prereleases }));
  expect(invoke).toHaveBeenCalledWith('desktop_update_prereleases_set', { enabled: true });
  expect((screen.getByRole('switch', { name: en.update.setting.autoUpdate }) as HTMLButtonElement).disabled).toBe(true);
  projection.hostActions = ['check', 'prereleases', 'auto_update'];
});
