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

function expectNoUpdateControls() {
  expect(screen.queryByRole('button', { name: i18n.t('dashboard.upgradeNow') })).toBeNull();
  expect(screen.queryByText(i18n.t('dashboard.autoUpdate'))).toBeNull();
  expect(screen.queryByText(i18n.t('dashboard.checkFailed'))).toBeNull();
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
      i18n.t('dashboard.desktopManagedUpdateAvailable', { to: '10.0.0' }),
    )).toBeTruthy();
    expect(screen.queryByText(i18n.t('dashboard.checkFailed'))).toBeNull();
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
    expect(await screen.findByText(i18n.t('dashboard.upToDate'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('dashboard.checkFailed'))).toBeNull();
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
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.upgradeNow') }));
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
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.checkUpdate') }));
    expect(await screen.findByText(i18n.t('dashboard.desktopManagedUpdateAvailable', { to: '3.2.3' }))).toBeTruthy();
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
    expect(screen.queryByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateTitle') })).toBeNull();
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
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.upgradeNow') }));
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
    expect(await screen.findByText(i18n.t('dashboard.checkFailed'))).toBeTruthy();
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
    expect(await screen.findByText(i18n.t('dashboard.checkFailed'))).toBeTruthy();
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
    expect(await screen.findByText(i18n.t('dashboard.desktopManagedUpdateAvailable', { to: '3.2.3rc1' }))).toBeTruthy();
    expect(screen.getByText('3.2.3rc1')).toBeTruthy();
    expect(screen.queryByText(i18n.t('dashboard.desktopManagedUpdate'))).toBeNull();
    expectNoUpdateControls();
    expect(api.getVersion).toHaveBeenCalledOnce();
    expect(entry.querySelector('.animate-pulse')).toBeTruthy();
  });

  it('keeps neutral desktop guidance while the desktop app has published no conclusion', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: null, has_update: false, error: null, managed_by: 'desktop' });
    const entry = await openBrowserPopup('v3.2.2rc13');
    expect(await screen.findByText(i18n.t('dashboard.desktopManagedUpdate'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('dashboard.latestVersion'))).toBeNull();
    expect(screen.queryByText(i18n.t('dashboard.upToDate'))).toBeNull();
    expectNoUpdateControls();
    expect(entry.querySelector('.animate-pulse')).toBeNull();
  });

  it('rereads the version without the shared cache when the user refreshes', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: null, has_update: false, error: null, managed_by: 'desktop' });
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: '3.2.3', has_update: true, error: null, managed_by: 'desktop' });
    await openBrowserPopup('v3.2.2rc13');
    expect(api.getVersion).toHaveBeenLastCalledWith(undefined);
    fireEvent.click(screen.getByRole('button', { name: i18n.t('dashboard.checkUpdate') }));
    expect(await screen.findByText(i18n.t('dashboard.desktopManagedUpdateAvailable', { to: '3.2.3' }))).toBeTruthy();
    expect(api.getVersion).toHaveBeenLastCalledWith({ cache: false });
  });

  it('relays an up-to-date desktop app without update controls', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.2.2rc13', latest: '3.2.2-rc.13', has_update: false, error: null, managed_by: 'desktop' });
    const entry = await openBrowserPopup('v3.2.2rc13');
    expect(await screen.findByText(i18n.t('dashboard.upToDate'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('dashboard.desktopManagedUpdate'))).toBeNull();
    expectNoUpdateControls();
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
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.upgradeNow') }));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateConfirm') }));

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
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.upgradeNow') }));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateConfirm') }));

    expect(await screen.findByText(/deferred activation helper/)).toBeTruthy();
    expect(screen.queryByText('raw deferred helper transcript')).toBeNull();
  });

  it('keeps a clean successful banner when only raw output is returned', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '9.9.9', latest: '10.0.0', has_update: true });
    api.doUpgrade.mockResolvedValueOnce({
      ok: true,
      message: 'Upgrade successful',
      output: 'installer stdout\nShow Runtime prepared.',
      activation_notice: null,
      restarting: false,
    });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);

    fireEvent.click(await screen.findByTitle('v9.9.9'));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.upgradeNow') }));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateConfirm') }));

    expect(await screen.findByText(i18n.t('dashboard.upgradeSuccess'))).toBeTruthy();
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
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.upgradeNow') }));
    fireEvent.click(await screen.findByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateConfirm') }));

    expect(await screen.findByText(i18n.t('dashboard.upgradeFailed'))).toBeTruthy();
    expect(screen.queryByText('raw package-manager transcript')).toBeNull();
  });
});

// These contracts exercise the request/receipt boundary. Existing updater tests
// only covered native install/skip and cannot catch remote authorization,
// counted confirmation, or a disconnected host being mistaken for success.
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
  fireEvent.click(screen.getByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateTitle') }));
}

function confirmRequest() {
  fireEvent.click(screen.getByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateConfirm') }));
}

describe('remote desktop update contract', () => {
  it.each(['owner', 'member', 'editor', 'viewer'] as const)('projects %s capability to request controls and guidance', async (role) => {
    authorization.capabilities.can_manage_instance = role === 'owner' || role === 'member';
    authorization.capabilities.is_instance_owner = role === 'owner';
    await openBrowserPopup('v9.9.9');
    expect(Boolean(screen.queryByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateTitle') })))
      .toBe(role === 'owner' || role === 'member');
    if (role === 'editor' || role === 'viewer') {
      expect(screen.getByText(i18n.t('dashboard.desktopManagedUpdateMemberHint'))).toBeTruthy();
      expect(api.getConfig).not.toHaveBeenCalled();
    }
    expect(api.requestDesktopUpdate).not.toHaveBeenCalled();
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  it('checks work on first submit, displays nested counts/unknown, and retries explicitly confirmed', async () => {
    api.requestDesktopUpdate.mockResolvedValueOnce({ ok: false, error: 'confirmation_required', counts: {
      agents: 2, watches: 3, tasks: 4, total: 9, unknown: true,
    } });
    await requestFromBrowser();
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateCheckWork'))).toBeTruthy();
    confirmRequest();
    expect(await screen.findByText(i18n.t('dashboard.desktopRemoteUpdateInterruptions', { agents: 2, watches: 3, tasks: 4 }))).toBeTruthy();
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateUnknownWork'))).toBeTruthy();
    expect(api.requestDesktopUpdate).toHaveBeenLastCalledWith('10.0.0', { confirmInterrupt: false });
    api.getVersion.mockResolvedValue(remoteReceipt('awaiting_host'));
    fireEvent.click(screen.getByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateConfirmInterrupt') }));
    expect(await screen.findByText(i18n.t('dashboard.desktopRemoteUpdateAwaitingHost'))).toBeTruthy();
    expect(api.requestDesktopUpdate).toHaveBeenLastCalledWith('10.0.0', { confirmInterrupt: true });
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  it.each(['prompt', 'install'] as const)('shows %s delivery before submitting and follows the authoritative result', async (delivery) => {
    api.getVersion.mockResolvedValue({ current: '9.9.9', latest: '10.0.0-rc.1', has_update: true, managed_by: 'desktop', remote_install_allowed: delivery === 'install' });
    api.requestDesktopUpdate.mockResolvedValue({ ok: true, request_id: 'request-1', delivery });
    await requestFromBrowser();
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateConfirmDescription', { delivery: i18n.t(delivery === 'install'
      ? 'dashboard.desktopRemoteUpdateDeliveryInstall' : 'dashboard.desktopRemoteUpdateDeliveryPrompt') }))).toBeTruthy();
    api.getVersion.mockResolvedValue({ ...remoteReceipt(delivery === 'install' ? 'downloading' : 'awaiting_host', 42), remote_install_allowed: delivery === 'install' });
    confirmRequest();
    expect(await screen.findByText(i18n.t(delivery === 'install' ? 'dashboard.desktopRemoteUpdateDownloading' : 'dashboard.desktopRemoteUpdateAwaitingHost', { progress: 42 }))).toBeTruthy();
    // SemVer is sent unchanged, even though the badge normalizes display strings.
    expect(api.requestDesktopUpdate).toHaveBeenCalledWith('10.0.0-rc.1', { confirmInterrupt: false });
  });

  it.each(['failed', 'declined', 'expired'] as const)('renders terminal %s from a receipt', async (phase) => {
    api.getVersion.mockResolvedValue(remoteReceipt(phase));
    await openBrowserPopup('v9.9.9');
    const key = phase === 'failed' ? 'dashboard.desktopRemoteUpdateFailed' : phase === 'declined'
      ? 'dashboard.desktopRemoteUpdateDeclined' : 'dashboard.desktopRemoteUpdateExpired';
    expect(screen.getByText(i18n.t(key))).toBeTruthy();
    expect(api.getVersion).toHaveBeenCalledTimes(1);
  });

  it('reconnects after disconnection and requires both the same receipt and resulting version', async () => {
    api.getVersion.mockResolvedValue(remoteReceipt('installing'));
    await openTimingPopup();
    api.getVersion.mockRejectedValue(new Error('disconnected'));
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateRestarting'))).toBeTruthy();
    api.getVersion.mockResolvedValue({ ...remoteReceipt('completed'), current: '10.0.0' });
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateCompleted', { target: '10.0.0' }))).toBeTruthy();
    expect(screen.queryByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateTitle') })).toBeNull();
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
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateRestarting'))).toBeTruthy();
    await act(() => vi.advanceTimersByTimeAsync(125_000));
    expect(screen.queryByText(i18n.t('dashboard.desktopRemoteUpdateTimedOut'))).toBeNull();
    expect(screen.getByText(i18n.t(phase === 'downloading'
      ? 'dashboard.desktopRemoteUpdateDownloadingUnknown'
      : 'dashboard.desktopRemoteUpdateAwaitingHost'))).toBeTruthy();

    // A second outage must have its own bounded recovery and still cannot
    // extend the original deadline, even with repeated healthy receipt reads.
    api.getVersion.mockRejectedValueOnce(new Error('disconnected again'));
    await act(() => vi.advanceTimersByTimeAsync(2000));
    await act(() => vi.advanceTimersByTimeAsync(125_000));
    expect(screen.queryByText(i18n.t('dashboard.desktopRemoteUpdateTimedOut'))).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(35 * 60_000 - 10 * 60_000 - 254_000 - 1));
    expect(screen.queryByText(i18n.t('dashboard.desktopRemoteUpdateTimedOut'))).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateTimedOut'))).toBeTruthy();
    const calls = api.getVersion.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(api.getVersion).toHaveBeenCalledTimes(calls);
  });

  it.each(['unreachable', 'stuck', 'wrong-version', 'hanging-read'] as const)('bounds %s reconnect and never claims success', async (mode) => {
    const info = remoteReceipt(mode === 'wrong-version' ? 'completed' : 'restarting');
    api.getVersion.mockResolvedValue(info);
    await openTimingPopup();
    if (mode === 'wrong-version') {
      expect((screen.getByRole('button', {
        name: i18n.t('dashboard.desktopRemoteUpdateTitle'),
      }) as HTMLButtonElement).disabled).toBe(true);
    }
    if (mode === 'unreachable') api.getVersion.mockRejectedValue(new Error('offline'));
    if (mode === 'hanging-read') api.getVersion.mockImplementation(() => new Promise(() => {}));
    await act(() => vi.advanceTimersByTimeAsync(125_000));
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateTimedOut'))).toBeTruthy();
    expect(screen.queryByText(i18n.t('dashboard.desktopRemoteUpdateCompleted', { target: '10.0.0' }))).toBeNull();
    const calls = api.getVersion.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(api.getVersion).toHaveBeenCalledTimes(calls);
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  it.each(['not_desktop_managed', 'no_update_available', 'update_in_progress', 'desktop_shell_unavailable'] as const)('handles %s without falling back to Runtime upgrade', async (error) => {
    api.requestDesktopUpdate.mockResolvedValue({ ok: false, error });
    await requestFromBrowser();
    confirmRequest();
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(api.doUpgrade).not.toHaveBeenCalled();
  });

  it.each(['failed', 'declined', 'expired'] as const)('allows retry when a manual refresh discovers %s after timeout', async (phase) => {
    api.getVersion.mockResolvedValue(remoteReceipt('restarting'));
    await openTimingPopup();
    await act(() => vi.advanceTimersByTimeAsync(125_000));
    expect(screen.getByText(i18n.t('dashboard.desktopRemoteUpdateTimedOut'))).toBeTruthy();
    api.getVersion.mockResolvedValue(remoteReceipt(phase));
    fireEvent.click(screen.getByRole('button', { name: i18n.t('dashboard.checkUpdate') }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(screen.queryByText(i18n.t('dashboard.desktopRemoteUpdateTimedOut'))).toBeNull();
    const requestButton = screen.getByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateTitle') });
    expect((requestButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(requestButton);
    expect(screen.getByRole('dialog')).toBeTruthy();
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
    expect((screen.getByRole('button', { name: i18n.t('dashboard.checkUpdate') }) as HTMLButtonElement).disabled).toBe(true);
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
    const refresh = screen.getByRole('button', { name: i18n.t('dashboard.checkUpdate') });
    expect((refresh as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(refresh);
    await vi.waitFor(() => expect(desktopInvoke).toHaveBeenCalledWith('desktop_update_check'));
  });

  it('keeps the Member pure-Web upgrade and handles its counted confirmation', async () => {
    authorization.capabilities.is_instance_owner = false;
    api.getVersion.mockResolvedValue({ current: '9.9.9', latest: '10.0.0', has_update: true });
    api.doUpgrade.mockResolvedValueOnce({ ok: false, error: 'confirmation_required', counts: { agents: 1, watches: 0, tasks: 2, total: 3, unknown: false } });
    await openBrowserPopup('v9.9.9');
    fireEvent.click(screen.getByRole('button', { name: i18n.t('dashboard.upgradeNow') }));
    confirmRequest();
    expect(await screen.findByText(i18n.t('dashboard.desktopRemoteUpdateInterruptions', { agents: 1, watches: 0, tasks: 2 }))).toBeTruthy();
    expect(api.doUpgrade).toHaveBeenLastCalledWith({ confirmInterrupt: false });
    fireEvent.click(screen.getByRole('button', { name: i18n.t('dashboard.desktopRemoteUpdateConfirmInterrupt') }));
    await vi.waitFor(() => expect(api.doUpgrade).toHaveBeenLastCalledWith({ confirmInterrupt: true }));
    expect(api.requestDesktopUpdate).not.toHaveBeenCalled();
  });

  it.each(['editor', 'viewer'])('withholds pure-Web upgrade and config reads from %s', async () => {
    authorization.capabilities.can_manage_instance = false;
    api.getVersion.mockResolvedValue({ current: '9.9.9', latest: '10.0.0', has_update: true });
    await openBrowserPopup('v9.9.9');
    expect(screen.queryByRole('button', { name: i18n.t('dashboard.upgradeNow') })).toBeNull();
    expect(api.getConfig).not.toHaveBeenCalled();
  });
});
