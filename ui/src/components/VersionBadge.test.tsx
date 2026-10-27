// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { VersionBadge } from './VersionBadge';

const api = vi.hoisted(() => ({
  getVersion: vi.fn(async () => ({ current: '9.9.9', latest: '10.0.0', has_update: true, managed_by: 'desktop' })),
  getConfig: vi.fn(async () => ({ update: { auto_update: true } })),
  doUpgrade: vi.fn(async () => ({ ok: true, message: 'Upgrade successful', output: null, restarting: false })),
}));
const desktopInvoke = vi.hoisted(() => vi.fn(async (command: string) => {
  if (command === 'desktop_update_state') {
    return { phase: 'available', latest: '10.0.0', channel: 'stable', skippedVersion: null };
  }
  return undefined;
}));
vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../lib/useIsDesktop', () => ({ useIsDesktop: () => true }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_VERSION__?: string }).__AVIBE_DESKTOP_VERSION__;
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
  },
) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
  Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: appVersion, configurable: true });
  if (withUpdater) {
    Object.defineProperty(window, '__TAURI_INTERNALS__', {
      value: { invoke: desktopInvoke },
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

  it('opens the Web popover in the desktop shell instead of navigating to the update deep link', async () => {
    renderInShell('3.2.2-rc.13', '3.2.2rc13', '3.2.3');
    const entry = await screen.findByTitle('v3.2.2-rc.13');
    const currentUrl = window.location.href;
    fireEvent.click(entry);
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(window.location.href).toBe(currentUrl);
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
  });

  it('renders the desktop updater error state in the popover', async () => {
    desktopInvoke.mockImplementation(async (command: string) => {
      if (command === 'desktop_update_state') {
        return { phase: 'error', latest: null, channel: 'stable', skippedVersion: null };
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
        return { phase: 'disabled', latest: null, channel: 'stable', skippedVersion: null };
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

    expect(await screen.findByText(i18n.t('dashboard.upgradeFailed'))).toBeTruthy();
    expect(screen.queryByText('raw package-manager transcript')).toBeNull();
  });
});
