// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { VersionBadge } from './VersionBadge';

const api = vi.hoisted(() => ({
  getVersion: vi.fn(async () => ({ current: '9.9.9', latest: '10.0.0', has_update: true, managed_by: 'desktop' })),
  getConfig: vi.fn(async () => ({ update: { auto_update: true } })),
}));
vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../lib/useIsDesktop', () => ({ useIsDesktop: () => true }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_MANAGED_CONNECTION__?: boolean }).__AVIBE_DESKTOP_MANAGED_CONNECTION__;
  delete (window as { __AVIBE_DESKTOP_VERSION__?: string }).__AVIBE_DESKTOP_VERSION__;
});

describe('desktop version ownership', () => {
  it('uses the stamped app version and checks the connected service without configuring the Python updater', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: '3.1.2-rc.15', configurable: true });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
    expect(screen.getByTitle('v3.1.2-rc.15')).toBeTruthy();
    await waitFor(() => expect(api.getVersion).toHaveBeenCalledOnce());
    expect(api.getConfig).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('identifies an independently managed service separately from the updated shell', async () => {
    api.getVersion.mockResolvedValueOnce({ current: '3.1.1', latest: '3.1.2', has_update: true, managed_by: undefined as unknown as string });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: '3.1.2', configurable: true });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
    const entry = await screen.findByTitle(i18n.t('dashboard.desktopServiceVersion', { desktop: '3.1.2', service: '3.1.1' }));
    expect(entry.textContent).toContain(i18n.t('dashboard.independentServiceVersion', { version: '3.1.1' }));
    expect(entry.querySelector('.animate-pulse')).toBeNull();
    expect(api.getConfig).not.toHaveBeenCalled();
  });

  it('does not label a bundled UI serving an external controller as a managed service', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: '3.1.2', configurable: true });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
    await waitFor(() => expect(api.getVersion).toHaveBeenCalledOnce());
    Object.defineProperty(window, '__AVIBE_DESKTOP_MANAGED_CONNECTION__', { value: false, configurable: true });
    fireEvent(window, new Event('avibe:runtime-management'));
    const entry = await screen.findByTitle(i18n.t('dashboard.desktopServiceVersion', { desktop: '3.1.2', service: i18n.t('dashboard.unknownRevision') }));
    expect(entry.textContent).not.toContain('9.9.9');
  });

  it('retains the browser version entry and its existing managed-runtime guidance', async () => {
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
    const entry = await screen.findByTitle('v9.9.9');
    fireEvent.click(entry);
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(api.getVersion).toHaveBeenCalledOnce();
    expect(entry.querySelector('.animate-pulse')).toBeNull();
  });
});
