/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { DesktopDownloadCta } from './DesktopDownloadCta';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderCta = () => render(
  <I18nextProvider i18n={i18n}>
    <DesktopDownloadCta />
  </I18nextProvider>,
);

const setNavigator = (platform: string, userAgent: string, maxTouchPoints = 0) => {
  Object.defineProperties(window.navigator, {
    platform: { configurable: true, value: platform },
    userAgent: { configurable: true, value: userAgent },
    maxTouchPoints: { configurable: true, value: maxTouchPoints },
  });
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('DesktopDownloadCta', () => {
  it('preselects macOS and exposes both chip targets', async () => {
    setNavigator('MacIntel', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)');
    const user = userEvent.setup();
    renderCta();

    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('macos');
    expect(screen.getByRole('radio', { name: 'Apple Silicon' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('link', { name: /Download desktop app/i }).getAttribute('href')).toBe(
      'https://dl.avibe.bot/latest/Avibe_aarch64-apple-darwin.dmg',
    );

    await user.click(screen.getByRole('radio', { name: 'Intel' }));
    expect(screen.getByRole('link', { name: /Download desktop app/i }).getAttribute('href')).toBe(
      'https://dl.avibe.bot/latest/Avibe_x86_64-apple-darwin.dmg',
    );
  });

  it('switches from Windows to macOS and keeps the Windows target exact', async () => {
    setNavigator('Win32', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)');
    const user = userEvent.setup();
    renderCta();

    expect(screen.getByRole('link', { name: /Download desktop app/i }).getAttribute('href')).toBe(
      'https://dl.avibe.bot/latest/Avibe_x86_64-pc-windows-msvc.exe',
    );
    await user.selectOptions(screen.getByRole('combobox'), 'macos');
    expect(screen.getByRole('radio', { name: 'Apple Silicon' })).not.toBeNull();
    expect(screen.getByRole('link', { name: /Download desktop app/i }).getAttribute('href')).toBe(
      'https://dl.avibe.bot/latest/Avibe_aarch64-apple-darwin.dmg',
    );
  });

  it('requires a platform choice when detection is unsupported', async () => {
    setNavigator('Linux x86_64', 'Mozilla/5.0 (X11; Linux x86_64)');
    const user = userEvent.setup();
    renderCta();

    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('unknown');
    expect((screen.getByRole('button', { name: /Download desktop app/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('link', { name: /Download desktop app/i })).toBeNull();

    await user.selectOptions(screen.getByRole('combobox'), 'windows');
    expect(screen.getByRole('link', { name: /Download desktop app/i }).getAttribute('href')).toBe(
      'https://dl.avibe.bot/latest/Avibe_x86_64-pc-windows-msvc.exe',
    );
  });
});
