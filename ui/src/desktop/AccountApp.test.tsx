// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../i18n/en.json';
import { AccountApp } from './AccountApp';

const send = vi.hoisted(() => vi.fn(async () => null));
const update = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
const bridge = vi.hoisted(() => ({ failed: false, error: null as string | null }));
// What the shell reports for the selected instance; absent from a shell that predates it.
const shown = vi.hoisted(() => ({ instance: undefined as unknown }));
vi.mock('./useAccountClient', () => ({
  useAccountClient: () => bridge.failed ? { error: 'bridge_unavailable', send, state: null } : ({
    error: bridge.error,
    send,
    state: {
      version: 1, revision: 1, generation: 4,
      account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' }, email: null, gate: null, methods: [] },
      connection: { status: 'remote_connected', committed: { kind: 'remote', name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio.example' }, pending: null },
      enrollment: { status: 'failed', instance_id: null, error_code: 'persistence_failed' },
      catalog: { status: 'idle', instances: [] },
      local: { name: '小麦的 Mac', remote_access: 'off' },
      desktop: {
        version: '3.2.6',
        update: { phase: 'available', latest: '3.2.7', channel: 'stable', prereleases: false, autoUpdate: false, hostActions: ['check', 'prereleases', 'auto_update'], skippedVersion: null, interactive: true, ...update.value },
      },
      consent: null,
      remote_auxiliary: 'disabled_incomplete',
      ...(shown.instance === undefined ? {} : { instance_update: shown.instance }),
    },
  }),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); update.value = {}; bridge.failed = false; bridge.error = null; shown.instance = undefined; window.history.replaceState(null, '', '/'); });

// ui/design: update/panel/trusted-view
it('AUTH-SETUP-178 the trusted update view acts on this computer\'s desktop app with the same panel', async () => {
  window.history.replaceState(null, '', '/account/index.html?view=updates');
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  expect(screen.getByText(en.update.trustedView.title)).toBeTruthy();
  expect(screen.getByText(en.update.status.available)).toBeTruthy();
  expect(screen.queryByText(en.desktopClient.errors.request)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: en.update.action.update }));
  expect(send).toHaveBeenCalledWith({ type: 'update_install', target_version: '3.2.7' });
});

it('AUTH-SETUP-178 the trusted update view keeps an accepted install failure distinct from a failed check', async () => {
  window.history.replaceState(null, '', '/account/index.html?view=updates');
  send.mockResolvedValue({ ok: true, state: { generation: 4 } } as never);
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  const rendered = render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  fireEvent.click(screen.getByRole('button', { name: en.update.action.update }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: 'update_install', target_version: '3.2.7' }));
  update.value = { phase: 'error', latest: null };
  await act(async () => { rendered.rerender(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>); });
  expect(await screen.findByText(en.update.failure.install.title)).toBeTruthy();
  expect(screen.queryByText(en.update.status.checkFailed)).toBeNull();
});

it('AUTH-SETUP-178 a rejected trusted install does not turn a later check error into install failure', async () => {
  window.history.replaceState(null, '', '/account/index.html?view=updates');
  send.mockResolvedValue(null);
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  const rendered = render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  fireEvent.click(screen.getByRole('button', { name: en.update.action.update }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: 'update_install', target_version: '3.2.7' }));
  update.value = { phase: 'error', latest: null };
  await act(async () => { rendered.rerender(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>); });
  expect(await screen.findByText(en.update.status.checkFailed)).toBeTruthy();
  expect(screen.queryByText(en.update.failure.install.title)).toBeNull();
});

// ui/design: chrome/remote-update-popup. While the main window shows another computer, the
// shell opens this page in its own popup window: the same panel, closed by Escape or a click
// outside it, never sending an account action (the shell refuses those from this window).
it('AUTH-SETUP-178 the update popup is the same panel, closed by Escape or a click outside', async () => {
  window.history.replaceState(null, '', '/account/index.html?view=update-popover');
  const invoke = vi.fn(async () => undefined);
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  const dialog = screen.getByRole('dialog', { name: en.dashboard.versionAndUpdate });
  expect(screen.queryByText(en.update.trustedView.title)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: en.update.action.update }));
  expect(send).toHaveBeenCalledWith({ type: 'update_install', target_version: '3.2.7' });
  fireEvent.click(dialog);
  expect(invoke).not.toHaveBeenCalled();
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(invoke).toHaveBeenCalledWith('plugin:window|close', { label: 'update-popover' });
  invoke.mockClear();
  fireEvent.click(document.querySelector('[data-update-popover]')!);
  expect(invoke).toHaveBeenCalledWith('plugin:window|close', { label: 'update-popover' });
  expect(send).not.toHaveBeenCalledWith({ type: 'cancel' });
});

it('AUTH-SETUP-178 the update popup still shows why it cannot reach the desktop app', () => {
  window.history.replaceState(null, '', '/account/index.html?view=update-popover');
  bridge.failed = true;
  render(<AccountApp />);
  expect(screen.getByRole('dialog', { name: 'dashboard.versionAndUpdate' })).toBeTruthy();
  expect(screen.getByText('desktopClient.errors.bridge')).toBeTruthy();
});

// The remote note lives in the instance menu only, where switching happens; full pages do not repeat it.
it('AUTH-SETUP-179 full pages do not repeat the remote-instance note', async () => {
  window.history.replaceState(null, '', '/account/index.html?view=instances');
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  expect(screen.queryByText(en.desktopClient.auxiliaryIncomplete)).toBeNull();
});

// ui/design: update/instance-and-app. The owner clicked Update here meaning the instance he was looking at,
// and the app on this computer was updated instead: the panel had one "Update" and no owner on it.
const instanceReady = (extra: Record<string, unknown> = {}) => ({
  instance_id: 'studio', status: 'ready', can_manage: true, request: null, fetched_at: '2026-11-20T07:00:00Z',
  snapshot: { current: '3.2.5', latest: '3.2.7', has_update: true, error: null, prereleases: false, auto_update: false },
  ...extra,
});
async function openPopup() {
  window.history.replaceState(null, '', '/account/index.html?view=update-popover');
  send.mockResolvedValue({ ok: true, state: { generation: 4 } } as never);
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  const section = (kind: string) => document.querySelector<HTMLElement>(`[data-update-subject="${kind}"]`);
  return { instance: section('instance'), app: section('app') };
}

it("the popup on another computer's instance updates the instance from its own section, and the app from its own", async () => {
  shown.instance = instanceReady();
  const { instance, app } = await openPopup();
  // The instance first, by its name and with its own version; then this computer's app with its own.
  expect(instance!.textContent).toContain('Studio');
  expect(instance!.textContent).toContain('3.2.5');
  expect(app!.textContent).toContain(en.update.subject.app);
  expect(app!.textContent).toContain('3.2.6');
  expect(screen.queryByRole('button', { name: en.update.action.update })).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: 'Update Studio' }));
  // Exactly the version on screen, to the instance; nothing to this computer's updater.
  await waitFor(() => expect(send).toHaveBeenCalledExactlyOnceWith({ type: 'instance_update_install', target_version: '3.2.7' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Update this app' }));
  await waitFor(() => expect(send).toHaveBeenLastCalledWith({ type: 'update_install', target_version: '3.2.7' }));
  expect(send).toHaveBeenCalledTimes(2);
});

it('one check in the popup asks both updaters', async () => {
  shown.instance = instanceReady();
  await openPopup();
  fireEvent.click(screen.getByRole('button', { name: en.update.action.check }));
  await waitFor(() => expect(send.mock.calls.map(([action]) => (action as { type: string }).type)).toEqual(['update_check', 'instance_update_check']));
});

it("a member's check reads the instance again instead of asking for a check the shell would refuse", async () => {
  shown.instance = instanceReady({ can_manage: false });
  await openPopup();
  fireEvent.click(screen.getByRole('button', { name: en.update.action.check }));
  await waitFor(() => expect(send.mock.calls.map(([action]) => (action as { type: string }).type)).toEqual(['update_check', 'instance_update_refresh']));
});

it("a failed install of the instance is told by its section, not also by this page's connection notice", async () => {
  // The shell's receipt says the install failed; the client reports the same refusal as an error code.
  shown.instance = instanceReady({ request: { action: 'install', phase: 'failed', target: '3.2.7', error_code: 'install_failed', updated_at: new Date(Date.now() + 60_000).toISOString() } });
  bridge.error = 'install_failed';
  const { instance } = await openPopup();
  expect(instance!.textContent).toContain(en.update.failure.install.title);
  fireEvent.click(screen.getByRole('button', { name: en.update.action.retry }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: 'instance_update_install', target_version: '3.2.7' }));
  expect(screen.queryByText(en.desktopClient.errors.request)).toBeNull();
});

it("a refusal that leaves no receipt keeps this page's notice: the section has nothing to tell", async () => {
  // An older failed install is still the shell's last receipt; this click is refused before any new one.
  shown.instance = instanceReady({ request: { action: 'install', phase: 'failed', target: '3.2.7', error_code: 'install_failed', updated_at: '2026-11-20T07:03:17Z' } });
  bridge.error = 'busy';
  await openPopup();
  fireEvent.click(screen.getByRole('button', { name: en.update.action.retry }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: 'instance_update_install', target_version: '3.2.7' }));
  expect(screen.getByText(en.desktopClient.errors.request)).toBeTruthy();
});

it("a failed check or switch on the instance, which no section tells, keeps this page's notice", async () => {
  shown.instance = instanceReady({ request: { action: 'prereleases', phase: 'failed', target: null, error_code: 'request_failed', updated_at: '2026-11-20T07:03:17Z' } });
  bridge.error = 'request_failed';
  await openPopup();
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }));
  fireEvent.click(screen.getByRole('switch', { name: en.update.setting.prereleases }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: 'instance_update_set_prereleases', enabled: true }));
  expect(screen.getByText(en.desktopClient.errors.request)).toBeTruthy();
});

it('the update popup does not tell an enrollment failure', async () => {
  shown.instance = instanceReady();
  await openPopup();
  expect(screen.queryByRole('alert')).toBeNull();
});

it('an instance install sent from the popup shows as running, with no second Update for it', async () => {
  shown.instance = instanceReady({ request: { action: 'install', phase: 'sending', target: '3.2.7', error_code: null, updated_at: '2026-11-20T07:03:17Z' } });
  const { instance } = await openPopup();
  expect(instance!.textContent).toContain(en.update.status.installing);
  expect(screen.queryByRole('button', { name: 'Update Studio' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Update this app' })).toBeTruthy();
});

it('a member who cannot manage the instance sees its state and no way to change it', async () => {
  shown.instance = instanceReady({ can_manage: false });
  const { instance } = await openPopup();
  expect(instance!.textContent).toContain(en.update.status.available);
  expect(screen.queryByRole('button', { name: 'Update Studio' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }));
  expect(instance!.querySelector('[role="switch"]')).toBeNull();
});

it("on this computer's own instance, or under a shell that reports no instance, the popup stays the single panel", async () => {
  await openPopup();
  expect(document.querySelector('[data-update-subject]')).toBeNull();
  expect(screen.getByRole('button', { name: en.update.action.update })).toBeTruthy();
});

