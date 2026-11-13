// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { VersionBadge } from './VersionBadge';
import type { DesktopRemoteUpdateResult, VersionInfo, UpgradeResult } from '../context/ApiContext';
import {
  DESKTOP_OPEN_UPDATES_EVENT,
  DESKTOP_UPDATE_STATE_EVENT,
  type DesktopUpdateState,
} from '../lib/desktopShell';

const api = vi.hoisted(() => ({
  getVersion: vi.fn<() => Promise<Partial<VersionInfo> & Pick<VersionInfo, 'current' | 'latest' | 'has_update'>>>(async () => ({ current: '9.9.9', latest: '10.0.0', has_update: true, managed_by: 'desktop' })),
  getConfig: vi.fn(async () => ({ update: { auto_update: true } })),
  mutateConfig: vi.fn(async () => ({})),
  requestDesktopUpdate: vi.fn<(...args: unknown[]) => Promise<DesktopRemoteUpdateResult>>(),
  doUpgrade: vi.fn<(...args: unknown[]) => Promise<UpgradeResult>>(async () => ({ ok: true, message: 'Upgrade successful', output: null, restarting: false })),
}));
const defaultDesktopInvoke = vi.hoisted(() => async (command: string) => {
  if (command === 'desktop_update_state') {
    return { phase: 'available', latest: '10.0.0', channel: 'stable', skippedVersion: null, interactive: true };
  }
  if (command === 'plugin:event|listen') return 1;
  return undefined;
});
const desktopInvoke = vi.hoisted(() => vi.fn(defaultDesktopInvoke));
vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
const authorization = vi.hoisted(() => ({ remote: true, capabilities: { can_manage_instance: true, is_instance_owner: true } }));
vi.mock('../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => authorization }));
vi.mock('../lib/useIsDesktop', () => ({ useIsDesktop: () => true }));

beforeEach(() => {
  authorization.capabilities.can_manage_instance = true;
  authorization.capabilities.is_instance_owner = true;
  api.getVersion.mockReset().mockResolvedValue({ current: '9.9.9', latest: '10.0.0', has_update: true, managed_by: 'desktop' });
  api.requestDesktopUpdate.mockReset().mockResolvedValue({ ok: true, request_id: 'request-1', delivery: 'prompt' });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  vi.clearAllMocks();
  desktopInvoke.mockImplementation(defaultDesktopInvoke);
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_VERSION__?: string }).__AVIBE_DESKTOP_VERSION__;
  delete (window as { __AVIBE_DESKTOP_UPDATES_HANDLED__?: boolean }).__AVIBE_DESKTOP_UPDATES_HANDLED__;
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

function renderInShell(
  appVersion: string,
  runtimeVersion: string,
  latest: string | null = null,
  withUpdater = false,
  versionOverride?: {
    current: string;
    latest: string | null;
    has_update: boolean;
    error?: string | null;
    managed_by?: 'desktop';
    remote_request?: VersionInfo['remote_request'];
  },
) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
  Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: appVersion, configurable: true });
  if (withUpdater) {
    Object.defineProperty(window, '__TAURI_INTERNALS__', {
      value: {
        invoke: desktopInvoke,
        transformCallback: () => 1,
      },
      configurable: true,
    });
  }
  api.getVersion.mockResolvedValueOnce(versionOverride ?? {
    current: runtimeVersion,
    latest,
    has_update: latest !== null,
    managed_by: 'desktop',
  });
  render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
}

async function openBrowserPopup(title: string) {
  render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
  const entry = await screen.findByTitle(title);
  fireEvent.click(entry);
  expect(await screen.findByRole('dialog')).toBeTruthy();
  return entry;
}

function expectNoRuntimeControls() {
  expect(screen.queryByText(i18n.t('dashboard.autoUpdate'))).toBeNull();
  expect(screen.queryByText(i18n.t('update.status.checkFailed'))).toBeNull();
}

describe('desktop version ownership', () => {
  it('uses the stamped app version and never starts the Python updater', async () => {
    renderInShell('3.1.2-rc.15', '3.1.2rc15');
    await vi.waitFor(() => expect(api.getVersion).toHaveBeenCalledOnce());
    const entry = screen.getByTitle('v3.1.2-rc.15');
    expect(entry.className).not.toContain('text-gold-ink');
    expect(api.getConfig).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows the yellow badge in the desktop shell when the app reports an update', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', '3.2.3');
    await vi.waitFor(() => expect(api.getVersion).toHaveBeenCalledOnce());
    const entry = await screen.findByTitle('v3.2.2-rc.13');
    expect(entry.className).toContain('text-gold-ink');
    expect(entry.querySelector('.animate-pulse')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('prefers native available state over a Runtime version error', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', null, true, {
      current: '3.2.2rc13',
      latest: null,
      has_update: false,
      error: 'PyPI unavailable',
      managed_by: 'desktop',
    });
    fireEvent.click(await screen.findByTitle('v3.2.2-rc.13'));
    expect(await screen.findByText(
      i18n.t('update.status.available'),
    )).toBeTruthy();
    expect(screen.queryByText(i18n.t('update.status.checkFailed'))).toBeNull();
  });

  it('prefers native current state over a Runtime version error', async () => {
    desktopInvoke.mockImplementation(async (command: string) => {
      if (command === 'desktop_update_state') {
        return { phase: 'current', latest: '3.2.2', channel: 'stable', skippedVersion: null, interactive: false };
      }
      if (command === 'plugin:event|listen') return 1;
      return undefined;
    });
    renderInShell('3.2.2-rc.13', '3.2.2rc13', null, true, {
      current: '3.2.2rc13',
      latest: null,
      has_update: false,
      error: 'PyPI unavailable',
      managed_by: 'desktop',
    });
    fireEvent.click(await screen.findByTitle('v3.2.2-rc.13'));
    expect(await screen.findByText(i18n.t('update.status.current'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('update.status.checkFailed'))).toBeNull();
  });

  it('opens the Web popover in the desktop shell instead of navigating to the update deep link', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', '3.2.3');
    const entry = await screen.findByTitle('v3.2.2-rc.13');
    const currentUrl = window.location.href;
    fireEvent.click(entry);
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(window.location.href).toBe(currentUrl);
  });

  it('acknowledges the native open request when the desktop updater IPC is available', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', '3.2.3', true);
    await screen.findByTitle('v3.2.2-rc.13');
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith('desktop_update_state'));
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith(
      'plugin:event|listen',
      expect.objectContaining({
        event: DESKTOP_UPDATE_STATE_EVENT,
        target: { kind: 'Any' },
        handler: 1,
      }),
    ));
    const event = new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true });
    expect(window.dispatchEvent(event)).toBe(false);
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(window.__AVIBE_DESKTOP_UPDATES_HANDLED__).toBe(true);
  });

  it('registers the native updater state listener for every event target', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', '3.2.3', true);
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith(
      'plugin:event|listen',
      expect.objectContaining({
        event: DESKTOP_UPDATE_STATE_EVENT,
        target: { kind: 'Any' },
        handler: 1,
      }),
    ));
  });

  it('uses the desktop updater IPC for install and never calls the Runtime upgrade API', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', '3.2.3', true);
    fireEvent.click(await screen.findByTitle('v3.2.2-rc.13'));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('update.action.update') }));
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith('desktop_update_install'));
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  it('persists Skip this version through the desktop updater IPC', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', '3.2.3', true);
    fireEvent.click(await screen.findByTitle('v3.2.2-rc.13'));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.skipUpdate') }));
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith('desktop_update_skip'));
    expect(api.doUpgrade).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('suppresses a skipped release for automatic checks but offers it after an explicit refresh', async () => {
    let state = {
      phase: 'available',
      latest: '3.2.3',
      channel: 'stable',
      skippedVersion: '3.2.3',
      interactive: false,
    };
    desktopInvoke.mockImplementation(async (command: string) => {
      if (command === 'desktop_update_state') return state;
      if (command === 'desktop_update_check') {
        state = { ...state, interactive: true };
      }
      return undefined;
    });
    renderInShell('3.2.2-rc.13', '3.2.2rc13', null, true, {
      current: '3.2.2rc13',
      latest: null,
      has_update: false,
      managed_by: 'desktop',
    });

    const entry = await screen.findByTitle('v3.2.2-rc.13');
    expect(entry.className).not.toContain('text-gold-ink');

    fireEvent.click(entry);
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('update.action.check') }));
    expect(await screen.findByText(i18n.t('update.status.available'))).toBeTruthy();
    expect(entry.className).toContain('text-gold-ink');
  });

  it('falls back to Runtime version reads when an older shell lacks the updater command', async () => {
    desktopInvoke.mockImplementation(async (command: string) => {
      if (command === 'desktop_update_state') throw new Error('unknown command');
      return undefined;
    });
    renderInShell('3.2.2-rc.13', '3.2.2rc13', null, true, {
      current: '3.2.2rc13',
      latest: '3.2.3',
      has_update: true,
      managed_by: 'desktop',
    });

    const entry = await screen.findByTitle('v3.2.2-rc.13');
    await vi.waitFor(() => expect(entry.className).toContain('text-gold-ink'));
    expect(api.getVersion).toHaveBeenCalledWith({ cache: false });
    fireEvent.click(entry);
    expect(screen.queryByRole('button', { name: i18n.t('update.action.update') })).toBeNull();
    expect(api.requestDesktopUpdate).not.toHaveBeenCalled();
  });

  it('uses shell updater ownership even when the adopted Runtime omits managed_by', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', null, true, {
      current: '3.2.2rc13',
      latest: null,
      has_update: false,
    });
    const entry = await screen.findByTitle('v3.2.2-rc.13');
    await vi.waitFor(() => expect(entry.className).toContain('text-gold-ink'));
    fireEvent.click(entry);
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('update.action.update') }));
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith('desktop_update_install'));
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  it('renders the desktop updater error state in the popover', async () => {
    desktopInvoke.mockImplementation(async (command: string) => {
      if (command === 'desktop_update_state') {
        return { phase: 'error', latest: null, channel: 'stable', skippedVersion: null, interactive: false };
      }
      return undefined;
    });
    renderInShell('3.2.2-rc.13', '3.2.2rc13', null, true, {
      current: '3.2.2rc13',
      latest: null,
      has_update: false,
      error: 'check failed',
      managed_by: 'desktop',
    });
    fireEvent.click(await screen.findByTitle('v3.2.2-rc.13'));
    expect(await screen.findByText(i18n.t('update.status.checkFailed'))).toBeTruthy();
  });

  it('renders the desktop updater disabled state in the popover', async () => {
    desktopInvoke.mockImplementation(async (command: string) => {
      if (command === 'desktop_update_state') {
        return { phase: 'disabled', latest: null, channel: 'stable', skippedVersion: null, interactive: false };
      }
      return undefined;
    });
    renderInShell('3.2.2-rc.13', '3.2.2rc13', null, true, {
      current: '3.2.2rc13',
      latest: null,
      has_update: false,
      error: null,
      managed_by: 'desktop',
    });
    fireEvent.click(await screen.findByTitle('v3.2.2-rc.13'));
    expect(await screen.findByText(i18n.t('update.status.disabled'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('update.status.checkFailed'))).toBeNull();
  });

  it('shows and flags the Runtime version when the shell serves another one', async () => {
    renderInShell('3.1.2-rc.3', '3.1.2rc2');
    const entry = await screen.findByTitle(
      i18n.t('dashboard.runtimeVersionSkew', { app: '3.1.2-rc.3', runtime: '3.1.2rc2' }),
    );
    expect(entry.textContent).toBe('v3.1.2rc2');
    expect(entry.className).toContain('text-gold-ink');
    expect(api.getConfig).not.toHaveBeenCalled();
  });

  it('points a browser on a desktop Runtime to the update the desktop app found', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: '3.2.3-rc.1', has_update: true, managed_by: 'desktop' });
    const entry = await openBrowserPopup('v3.2.2rc13');
    expect(await screen.findByText(i18n.t('update.status.available'))).toBeTruthy();
    expect(screen.getByText('3.2.3rc1')).toBeTruthy();
    expect(screen.queryByText(i18n.t('dashboard.desktopManagedUpdate'))).toBeNull();
    expectNoRuntimeControls();
    // One Update button on every surface; from a browser it asks the host to install.
    expect(screen.getByRole('button', { name: i18n.t('update.action.update') })).toBeTruthy();
    expect(api.getVersion).toHaveBeenCalledOnce();
    expect(entry.querySelector('.animate-pulse')).toBeTruthy();
  });

  it('keeps neutral desktop guidance while the desktop app has published no conclusion', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: null, has_update: false, error: null, managed_by: 'desktop' });
    const entry = await openBrowserPopup('v3.2.2rc13');
    expect(await screen.findByText(i18n.t('dashboard.desktopManagedUpdate'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('update.row.offered'))).toBeNull();
    expect(screen.queryByText(i18n.t('update.status.current'))).toBeNull();
    expectNoRuntimeControls();
    expect(entry.querySelector('.animate-pulse')).toBeNull();
  });

  it('rereads the version without the shared cache when the user refreshes', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: null, has_update: false, error: null, managed_by: 'desktop' });
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: '3.2.3', has_update: true, error: null, managed_by: 'desktop' });
    await openBrowserPopup('v3.2.2rc13');
    expect(api.getVersion).toHaveBeenLastCalledWith(undefined);
    fireEvent.click(screen.getByRole('button', { name: i18n.t('update.action.refresh') }));
    expect(await screen.findByText(i18n.t('update.status.available'))).toBeTruthy();
    expect(api.getVersion).toHaveBeenLastCalledWith({ cache: false });
  });

  it('relays an up-to-date desktop app without update controls', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: '3.2.2-rc.13', has_update: false, error: null, managed_by: 'desktop' });
    const entry = await openBrowserPopup('v3.2.2rc13');
    expect(await screen.findByText(i18n.t('update.status.current'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('dashboard.desktopManagedUpdate'))).toBeNull();
    expectNoRuntimeControls();
    expect(entry.querySelector('.animate-pulse')).toBeNull();
  });

  it('withholds the auto-update toggle until the Runtime reports who manages it', async () => {
    let resolveVersion: (info: { current: string; latest: string; has_update: boolean; managed_by?: string }) => void = () => {};
    api.getVersion.mockImplementationOnce(() => new Promise((resolve) => { resolveVersion = resolve; }));
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
    fireEvent.click(screen.getByTitle('v...'));
    await vi.waitFor(() => expect(api.getConfig).toHaveBeenCalledOnce());
    // Let the loaded setting render while the version request is still pending.
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(screen.queryByText(i18n.t('dashboard.autoUpdate'))).toBeNull();

    resolveVersion({ current: '9.9.9', latest: '9.9.9', has_update: false });
    expect(await screen.findByText(i18n.t('dashboard.autoUpdate'))).toBeTruthy();
  });

  it('loads the auto-update setting when an editor is promoted to member', async () => {
    authorization.capabilities.can_manage_instance = false;
    authorization.capabilities.is_instance_owner = false;
    api.getVersion.mockResolvedValueOnce({ current: '9.9.9', latest: '9.9.9', has_update: false });
    const view = render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);

    fireEvent.click(await screen.findByTitle('v9.9.9'));
    expect(screen.queryByText(i18n.t('dashboard.autoUpdate'))).toBeNull();
    expect(api.getConfig).not.toHaveBeenCalled();

    authorization.capabilities.can_manage_instance = true;
    view.rerender(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);

    await vi.waitFor(() => expect(api.getConfig).toHaveBeenCalledOnce());
    expect(await screen.findByText(i18n.t('dashboard.autoUpdate'))).toBeTruthy();
  });

  it('shows a successful upgrade warning returned in the output', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '9.9.9', latest: '10.0.0', has_update: true });
    api.doUpgrade.mockResolvedValueOnce({
      ok: true,
      message: 'Upgrade successful',
      output: 'raw installer transcript',
      activation_notice: 'Stable launcher repair is still required.',
      restarting: false,
    });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);

    fireEvent.click(await screen.findByTitle('v9.9.9'));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('update.action.update') }));

    expect(await screen.findByText('Stable launcher repair is still required.')).toBeTruthy();
    expect(screen.queryByText('raw installer transcript')).toBeNull();
    expect(api.doUpgrade).toHaveBeenCalledOnce();
  });

  it('shows the deferred activation log notice without rendering raw output', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '9.9.9', latest: '10.0.0', has_update: true });
    api.doUpgrade.mockResolvedValueOnce({
      ok: true,
      message: 'Upgrade successful. Activation will complete after this process exits.',
      output: 'raw deferred helper transcript',
      activation_notice: 'The deferred activation helper will record its result in /tmp/upgrade.log.',
      restarting: false,
    });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);

    fireEvent.click(await screen.findByTitle('v9.9.9'));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('update.action.update') }));

    expect(await screen.findByText(/deferred activation helper/)).toBeTruthy();
    expect(screen.queryByText('raw deferred helper transcript')).toBeNull();
  });

  it('keeps a clean successful banner when only raw output is returned', async () => {
    // The successful install rereads the version; this Runtime is standalone throughout.
    api.getVersion.mockResolvedValue({ current: '9.9.9', latest: '10.0.0', has_update: true });
    api.doUpgrade.mockResolvedValueOnce({
      ok: true,
      message: 'Upgrade successful',
      output: 'installer stdout\nShow Runtime prepared.',
      activation_notice: null,
      restarting: false,
    });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);

    fireEvent.click(await screen.findByTitle('v9.9.9'));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('update.action.update') }));

    expect(await screen.findByText(i18n.t('update.status.updatedTo', { version: '10.0.0' }))).toBeTruthy();
    expect(screen.queryByText(/installer stdout/)).toBeNull();
    expect(screen.queryByText(/Show Runtime prepared/)).toBeNull();
  });

  it('keeps the localized failure banner free of raw output', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '9.9.9', latest: '10.0.0', has_update: true });
    api.doUpgrade.mockResolvedValueOnce({
      ok: false,
      message: '升级失败',
      output: 'raw package-manager transcript',
      activation_notice: null,
      restarting: false,
    });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);

    fireEvent.click(await screen.findByTitle('v9.9.9'));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('update.action.update') }));

    expect(await screen.findByText(i18n.t('update.failure.install.title'))).toBeTruthy();
    expect(screen.queryByText('raw package-manager transcript')).toBeNull();
  });
});

// These contracts exercise the request/receipt boundary. Existing updater tests
// only covered native install/skip and cannot catch remote authorization, a
// request that waits on a second confirmation, or a disconnected host being
// mistaken for success.
const remoteReceipt = (phase: NonNullable<VersionInfo['remote_request']>['phase'], progress: number | null = null): VersionInfo => ({
  current: '9.9.9', latest: '10.0.0', has_update: true, error: null, managed_by: 'desktop',
  remote_request: { request_id: 'request-1', target: '10.0.0', phase, progress, error_code: null, updated_at: '2026-10-29T02:00:00Z' },
});

async function openTimingPopup() {
  vi.useFakeTimers();
  render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
  await act(() => vi.advanceTimersByTimeAsync(0));
  fireEvent.click(screen.getByTitle('v9.9.9'));
}

async function requestFromBrowser() {
  await openBrowserPopup('v9.9.9');
  fireEvent.click(screen.getByRole('button', { name: i18n.t('update.action.update') }));
}

describe('remote desktop update contract', () => {
  it.each(['owner', 'member', 'editor', 'viewer'] as const)('projects %s capability to request controls and guidance', async (role) => {
    authorization.capabilities.can_manage_instance = role === 'owner' || role === 'member';
    authorization.capabilities.is_instance_owner = role === 'owner';
    await openBrowserPopup('v9.9.9');
    expect(Boolean(screen.queryByRole('button', { name: i18n.t('update.action.update') })))
      .toBe(role === 'owner' || role === 'member');
    if (role === 'editor' || role === 'viewer') {
      expect(screen.getByText(i18n.t('dashboard.desktopManagedUpdateMemberHint'))).toBeTruthy();
      expect(api.getConfig).not.toHaveBeenCalled();
    }
    expect(api.requestDesktopUpdate).not.toHaveBeenCalled();
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  // Clicking Update is the user's decision: one click sends the request, which
  // consents to interrupting active work, and no confirmation step follows.
  it.each(['prompt', 'install'] as const)('sends the %s request on one click and follows the authoritative result', async (delivery) => {
    api.getVersion.mockResolvedValue({ current: '9.9.9', latest: '10.0.0-rc.1', has_update: true, managed_by: 'desktop', remote_install_allowed: delivery === 'install' });
    api.requestDesktopUpdate.mockResolvedValue({ ok: true, request_id: 'request-1', delivery });
    await openBrowserPopup('v9.9.9');
    api.getVersion.mockResolvedValue({ ...remoteReceipt(delivery === 'install' ? 'downloading' : 'awaiting_host', 42), remote_install_allowed: delivery === 'install' });
    fireEvent.click(screen.getByRole('button', { name: i18n.t('update.action.update') }));
    expect(await screen.findByText(i18n.t(delivery === 'install' ? 'update.status.downloadingProgress' : 'update.status.awaitingHost', { progress: 42 }))).toBeTruthy();
    // SemVer is sent unchanged, even though the badge normalizes display strings.
    expect(api.requestDesktopUpdate).toHaveBeenCalledExactlyOnceWith('10.0.0-rc.1', { confirmInterrupt: true });
    // The version popover is the only dialog.
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
  });

  it.each(['failed', 'declined', 'expired'] as const)('renders terminal %s from a receipt', async (phase) => {
    api.getVersion.mockResolvedValue(remoteReceipt(phase));
    await openBrowserPopup('v9.9.9');
    const key = phase === 'failed' ? 'update.failure.failed.title' : phase === 'declined'
      ? 'update.failure.declined.title' : 'update.failure.expired.title';
    expect(screen.getByText(i18n.t(key))).toBeTruthy();
    expect(api.getVersion).toHaveBeenCalledTimes(1);
  });

  it('reconnects after disconnection and requires both the same receipt and resulting version', async () => {
    api.getVersion.mockResolvedValue(remoteReceipt('installing'));
    await openTimingPopup();
    api.getVersion.mockRejectedValue(new Error('disconnected'));
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText(i18n.t('update.status.restartingHost'))).toBeTruthy();
    api.getVersion.mockResolvedValue({ ...remoteReceipt('completed'), current: '10.0.0' });
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText(i18n.t('update.status.updatedTo', { version: '10.0.0' }))).toBeTruthy();
    expect(screen.queryByRole('button', { name: i18n.t('update.action.update') })).toBeNull();
    const calls = api.getVersion.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(api.getVersion).toHaveBeenCalledTimes(calls);
  });

  // Recovery must restore the request's original budget, not keep the outage
  // budget or start a fresh 35 minutes. Completion-only recovery misses both.
  it.each(['downloading', 'awaiting_host'] as const)('restores the original deadline after reconnecting to %s', async (phase) => {
    api.getVersion.mockResolvedValue(remoteReceipt(phase));
    await openTimingPopup();
    await act(() => vi.advanceTimersByTimeAsync(10 * 60_000));
    api.getVersion.mockRejectedValueOnce(new Error('disconnected'));
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText(i18n.t('update.status.restartingHost'))).toBeTruthy();
    await act(() => vi.advanceTimersByTimeAsync(125_000));
    expect(screen.queryByText(i18n.t('update.failure.timeout.title'))).toBeNull();
    expect(screen.getByText(i18n.t(phase === 'downloading'
      ? 'update.status.downloading'
      : 'update.status.awaitingHost'))).toBeTruthy();

    // A second outage must have its own bounded recovery and still cannot
    // extend the original deadline, even with repeated healthy receipt reads.
    api.getVersion.mockRejectedValueOnce(new Error('disconnected again'));
    await act(() => vi.advanceTimersByTimeAsync(2000));
    await act(() => vi.advanceTimersByTimeAsync(125_000));
    expect(screen.queryByText(i18n.t('update.failure.timeout.title'))).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(35 * 60_000 - 10 * 60_000 - 254_000 - 1));
    expect(screen.queryByText(i18n.t('update.failure.timeout.title'))).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByText(i18n.t('update.failure.timeout.title'))).toBeTruthy();
    const calls = api.getVersion.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(api.getVersion).toHaveBeenCalledTimes(calls);
  });

  it.each(['unreachable', 'stuck', 'wrong-version', 'hanging-read'] as const)('bounds %s reconnect and never claims success', async (mode) => {
    const info = remoteReceipt(mode === 'wrong-version' ? 'completed' : 'restarting');
    api.getVersion.mockResolvedValue(info);
    await openTimingPopup();
    if (mode === 'wrong-version') {
      expect(screen.getByText(i18n.t('update.status.verifying'))).toBeTruthy();
      expect(screen.queryByRole('button', { name: i18n.t('update.action.update') })).toBeNull();
    }
    if (mode === 'unreachable') api.getVersion.mockRejectedValue(new Error('offline'));
    if (mode === 'hanging-read') api.getVersion.mockImplementation(() => new Promise(() => {}));
    await act(() => vi.advanceTimersByTimeAsync(125_000));
    expect(screen.getByText(i18n.t('update.failure.timeout.title'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('update.status.updatedTo', { version: '10.0.0' }))).toBeNull();
    const calls = api.getVersion.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(api.getVersion).toHaveBeenCalledTimes(calls);
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  it.each(['not_desktop_managed', 'no_update_available', 'update_in_progress', 'desktop_shell_unavailable'] as const)('handles %s without falling back to Runtime upgrade', async (error) => {
    api.requestDesktopUpdate.mockResolvedValue({ ok: false, error });
    await requestFromBrowser();
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  it.each(['failed', 'declined', 'expired'] as const)('allows retry when a manual refresh discovers %s after timeout', async (phase) => {
    api.getVersion.mockResolvedValue(remoteReceipt('restarting'));
    await openTimingPopup();
    await act(() => vi.advanceTimersByTimeAsync(125_000));
    expect(screen.getByText(i18n.t('update.failure.timeout.title'))).toBeTruthy();
    api.getVersion.mockResolvedValue(remoteReceipt(phase));
    fireEvent.click(screen.getByRole('button', { name: i18n.t('update.action.refresh') }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(screen.queryByText(i18n.t('update.failure.timeout.title'))).toBeNull();
    const requestButton = screen.getByRole('button', { name: i18n.t('update.action.retry') });
    expect((requestButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(requestButton);
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(api.requestDesktopUpdate).toHaveBeenCalledExactlyOnceWith('10.0.0', { confirmInterrupt: true });
  });

  // Native settlement can precede the API status-file read; both a direct
  // Ignore snapshot and an unsolicited expiry event must release refresh.
  it.each(['ignore', 'expiry'] as const)('uses native %s settlement despite a stale API receipt', async (settlement) => {
    let state: DesktopUpdateState = {
      phase: 'available', latest: '10.0.0', channel: 'stable', interactive: true, skippedVersion: null,
      remoteRequest: { requestId: 'request-1', target: '10.0.0', phase: 'awaiting_host', progress: null, errorCode: null, updatedAt: '' },
    };
    desktopInvoke.mockImplementation(async (command: string) => {
      if (command === 'desktop_update_state') return state;
      if (command === 'desktop_update_remote_decline') {
        state = { ...state, remoteRequest: { ...state.remoteRequest!, phase: 'declined' } };
      }
      return undefined;
    });
    api.getVersion.mockResolvedValue(remoteReceipt('awaiting_host'));
    renderInShell('9.9.9', '9.9.9', '10.0.0', true, remoteReceipt('awaiting_host'));
    const transformCallback = vi.fn<(handler: (event: unknown) => void) => number>(() => 1);
    Object.assign(window.__TAURI_INTERNALS__!, { transformCallback });
    expect(await screen.findByText(i18n.t('dashboard.desktopRemoteUpdateLocalRequest', { target: '10.0.0' }))).toBeTruthy();
    await vi.waitFor(() => expect(api.getVersion).toHaveBeenCalledOnce());
    expect((screen.getByRole('button', { name: i18n.t('update.action.check') }) as HTMLButtonElement).disabled).toBe(true);
    if (settlement === 'ignore') {
      fireEvent.click(screen.getByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateIgnore') }));
      await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith('desktop_update_remote_decline'));
      await vi.waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      fireEvent.click(screen.getByTitle('v9.9.9'));
    } else {
      await vi.waitFor(() => expect(transformCallback).toHaveBeenCalledOnce());
      state = { ...state, remoteRequest: { ...state.remoteRequest!, phase: 'expired' } };
      act(() => transformCallback.mock.calls[0][0]({ payload: state }));
    }
    expect(desktopInvoke).not.toHaveBeenCalledWith('desktop_update_skip');
    expect(desktopInvoke).not.toHaveBeenCalledWith('desktop_update_check');
    const refresh = screen.getByRole('button', { name: i18n.t('update.action.check') });
    expect((refresh as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(refresh);
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith('desktop_update_check'));
  });

  it('updates the Runtime on one click for a Member, with no confirmation step', async () => {
    authorization.capabilities.is_instance_owner = false;
    api.getVersion.mockResolvedValue({ current: '9.9.9', latest: '10.0.0', has_update: true });
    let finish!: (result: UpgradeResult) => void;
    api.doUpgrade.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    await openBrowserPopup('v9.9.9');
    fireEvent.click(screen.getByRole('button', { name: i18n.t('update.action.update') }));
    expect(api.doUpgrade).toHaveBeenCalledExactlyOnceWith({ confirmInterrupt: true });
    // The version popover stays the only dialog and shows the request in flight.
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByText(i18n.t('update.status.installing'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: i18n.t('update.action.update') })).toBeNull();
    await act(async () => finish({ ok: true, message: 'Upgrade successful', output: null, restarting: false }));
    expect(await screen.findByText(i18n.t('update.status.updatedTo', { version: '10.0.0' }))).toBeTruthy();
    expect(api.doUpgrade).toHaveBeenCalledOnce();
    expect(api.requestDesktopUpdate).not.toHaveBeenCalled();
  });

  it.each(['editor', 'viewer'])('withholds pure-Web upgrade and config reads from %s', async () => {
    authorization.capabilities.can_manage_instance = false;
    api.getVersion.mockResolvedValue({ current: '9.9.9', latest: '10.0.0', has_update: true });
    await openBrowserPopup('v9.9.9');
    expect(screen.queryByRole('button', { name: i18n.t('update.action.update') })).toBeNull();
    expect(api.getConfig).not.toHaveBeenCalled();
  });
});

describe('pre-release updates', () => {
  const prereleaseSwitch = () => screen.findByRole('switch', { name: i18n.t('dashboard.prereleaseUpdates') });

  it('saves the Runtime setting, rechecks, and offers the release candidate with its notes', async () => {
    api.getConfig.mockResolvedValueOnce({ update: { auto_update: true, prereleases: false } });
    api.getVersion.mockResolvedValueOnce({ current: '3.2.5', latest: '3.2.5', has_update: false });
    await openBrowserPopup('v3.2.5');
    const toggle = await prereleaseSwitch();
    expect(toggle.getAttribute('aria-checked')).toBe('false');

    api.getVersion.mockResolvedValueOnce({ current: '3.2.5', latest: '3.2.6rc2', has_update: true });
    fireEvent.click(toggle);
    await vi.waitFor(() => expect(api.mutateConfig).toHaveBeenCalledWith([
      { kind: 'set', path: ['update', 'prereleases'], value: true },
    ]));
    await vi.waitFor(() => expect(api.getVersion).toHaveBeenLastCalledWith({ cache: false }));
    expect(await screen.findByText(i18n.t('dashboard.prereleaseBadge'))).toBeTruthy();
    const notes = screen.getByRole('link', { name: i18n.t('dashboard.releaseNotesFor', { version: '3.2.6rc2' }) });
    expect(notes.getAttribute('href')).toBe('https://github.com/avibe-bot/avibe/releases/tag/gh-v3.2.6rc2');
    expect((await prereleaseSwitch()).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('button', { name: i18n.t('update.action.update') })).toBeTruthy();
  });

  it('names the older stable release while running a pre-release with the switch off', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.2.6rc2', latest: '3.2.5', has_update: false });
    await openBrowserPopup('v3.2.6rc2');
    expect(await screen.findByText(i18n.t('dashboard.awaitingStableRelease'))).toBeTruthy();
    expect(screen.getByText(i18n.t('update.row.latestStable'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('update.status.current'))).toBeNull();
    expect(screen.queryByRole('button', { name: i18n.t('update.action.update') })).toBeNull();
  });

  it('sets the desktop app channel in the shell instead of the Runtime config', async () => {
    desktopInvoke.mockImplementation(async (command: string) => {
      if (command === 'desktop_update_state') {
        return { phase: 'current', latest: '3.2.5', channel: 'stable', prereleases: false, skippedVersion: null, interactive: false };
      }
      return defaultDesktopInvoke(command);
    });
    renderInShell('3.2.5', '3.2.5', null, true);
    fireEvent.click(await screen.findByTitle('v3.2.5'));
    fireEvent.click(await prereleaseSwitch());
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith('desktop_update_prereleases_set', { enabled: true }));
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });

  it('hides the switch under a desktop shell that predates it', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', '3.2.3', true);
    fireEvent.click(await screen.findByTitle('v3.2.2-rc.13'));
    expect(await screen.findByRole('button', { name: i18n.t('update.action.update') })).toBeTruthy();
    expect(screen.queryByRole('switch', { name: i18n.t('dashboard.prereleaseUpdates') })).toBeNull();
  });
});
