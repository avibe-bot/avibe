// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
  delete (window as { __AVIBE_DESKTOP_VERSION__?: string }).__AVIBE_DESKTOP_VERSION__;
});

describe('desktop version ownership', () => {
  it('uses the stamped app version and never starts the Python updater', () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_VERSION__', { value: '3.1.2-rc.15', configurable: true });
    render(<I18nextProvider i18n={i18n}><VersionBadge /></I18nextProvider>);
    expect(screen.getByTitle('v3.1.2-rc.15')).toBeTruthy();
    expect(api.getVersion).not.toHaveBeenCalled();
    expect(api.getConfig).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
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
