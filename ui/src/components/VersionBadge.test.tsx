// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { VersionBadge } from './VersionBadge';

const api = vi.hoisted(() => ({
  getVersion: vi.fn(async () => ({ current: '9.9.9', latest: '10.0.0', has_update: true, managed_by: 'desktop' })),
  getConfig: vi.fn(async () => ({ update: { auto_update: true } })),
  doUpgrade: vi.fn(async () => ({ ok: true, message: 'Upgrade successful', output: null, restarting: false })),
}));
vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../lib/useIsDesktop', () => ({ useIsDesktop: () => true }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_VERSION__?: string }).__AVIBE_DESKTOP_VERSION__;
});

function renderInShell(appVersion: string, runtimeVersion: string) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
  Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: appVersion, configurable: true });
  api.getVersion.mockResolvedValueOnce({ current: runtimeVersion, latest: null, has_update: false, managed_by: 'desktop' });
  render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
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

  it('shows and flags the Runtime version when the shell serves another one', async () => {
    renderInShell('3.1.2-rc.3', '3.1.2rc2');
    const entry = await screen.findByTitle(
      i18n.t('dashboard.runtimeVersionSkew', { app: '3.1.2-rc.3', runtime: '3.1.2rc2' }),
    );
    expect(entry.textContent).toBe('v3.1.2rc2');
    expect(entry.className).toContain('text-gold-ink');
    expect(api.getConfig).not.toHaveBeenCalled();
  });

  it('retains the browser version entry and its existing managed-runtime guidance', async () => {
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
    const entry = await screen.findByTitle('v9.9.9');
    fireEvent.click(entry);
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(api.getVersion).toHaveBeenCalledOnce();
    expect(entry.querySelector('.animate-pulse')).toBeNull();
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
});
