/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ mutateConfig: vi.fn() }));
const authorization = vi.hoisted(() => ({ capabilities: { can_manage_instance: true } }));
const media = vi.hoisted(() => ({
  prefersLight: false,
  listeners: new Set<() => void>(),
}));
vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      media: query,
      get matches() {
        return media.prefersLight;
      },
      addEventListener: (_type: string, listener: () => void) => media.listeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) => media.listeners.delete(listener),
    }),
  });
});

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));

import { ThemeProvider } from '../../context/ThemeProvider';
import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { SettingsGeneralPage } from './SettingsGeneralPage';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

const renderPage = () => render(
  <I18nextProvider i18n={i18n}>
    <ThemeProvider>
      <ToastProvider>
        <SettingsGeneralPage />
      </ToastProvider>
    </ThemeProvider>
  </I18nextProvider>,
);

const themeCard = (name: string) => screen.getByRole('radio', { name: new RegExp(name) });
// Located by role, not by label: the label is itself translated the moment the
// pick lands, which is part of what these tests are checking.
const languageSelect = () => screen.getByRole('combobox') as HTMLSelectElement;
const flip = (prefersLight: boolean) => act(() => {
  media.prefersLight = prefersLight;
  media.listeners.forEach((listener) => listener());
});

/** Hands back control of when each save settles, so a race can be staged. */
const deferSaves = () => {
  const pending: { resolve: () => void; reject: () => void }[] = [];
  api.mutateConfig.mockImplementation(() => new Promise<void>((resolve, reject) => {
    pending.push({ resolve: () => resolve(), reject: () => reject(new Error('offline')) });
  }));
  return pending;
};

beforeEach(async () => {
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  media.prefersLight = false;
  media.listeners.clear();
  authorization.capabilities.can_manage_instance = true;
  api.mutateConfig.mockReset();
  api.mutateConfig.mockResolvedValue(undefined);
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('General settings — language', () => {
  it('applies a pick to the interface and saves it without a Save button', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.selectOptions(languageSelect(), 'zh');

    await waitFor(() => expect(screen.getByText(zh.settings.general.title)).toBeTruthy());
    expect(api.mutateConfig).toHaveBeenCalledWith([
      expect.objectContaining({ path: ['language'], value: 'zh' }),
    ]);
    expect(screen.getByText(zh.settings.general.autosaved)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /save/i })).toBeNull();
  });

  it('lets a member change their own interface language without writing instance config', async () => {
    authorization.capabilities.can_manage_instance = false;
    const user = userEvent.setup();
    renderPage();

    await user.selectOptions(languageSelect(), 'zh');

    await waitFor(() => expect(screen.getByText(zh.settings.general.title)).toBeTruthy());
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });

  it('says so when the instance save fails, and retries the whole pick', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    renderPage();

    await user.selectOptions(languageSelect(), 'zh');
    await act(async () => { saves[0].reject(); });

    // The page promises changes save themselves, so a failure cannot be silent.
    expect(await screen.findByText(zh.settings.general.languageSaveFailed)).toBeTruthy();
    // The local switch stands — the pick they can see is the one they made.
    expect(languageSelect().value).toBe('zh');

    await user.click(screen.getByRole('button', { name: zh.common.retry }));
    await waitFor(() => expect(api.mutateConfig).toHaveBeenCalledTimes(2));
    expect(api.mutateConfig.mock.calls[1][0]).toEqual([
      expect.objectContaining({ path: ['language'], value: 'zh' }),
    ]);
  });

  it('settles on the last pick when they come faster than the saves', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    renderPage();

    await user.selectOptions(languageSelect(), 'zh');
    await user.selectOptions(languageSelect(), 'en');
    // Saves run one at a time, so the second has not been sent yet.
    expect(api.mutateConfig).toHaveBeenCalledTimes(1);

    await act(async () => { saves[0].reject(); });
    await waitFor(() => expect(api.mutateConfig).toHaveBeenCalledTimes(2));
    await act(async () => { saves[1].resolve(); });

    // The instance ends on what the user actually settled on, and the failure of
    // a language they already left is not reported at them.
    expect(api.mutateConfig.mock.calls[1][0]).toEqual([
      expect.objectContaining({ path: ['language'], value: 'en' }),
    ]);
    expect(screen.queryByText(en.settings.general.languageSaveFailed)).toBeNull();
    expect(languageSelect().value).toBe('en');
  });
});

describe('General settings — appearance', () => {
  it('persists an explicit theme and stops following the system once it is chosen', async () => {
    const user = userEvent.setup();
    renderPage();

    expect(themeCard(en.settings.general.appearanceSystem).getAttribute('aria-checked')).toBe('true');

    await user.click(themeCard(en.settings.general.appearanceLight));

    expect(themeCard(en.settings.general.appearanceLight).getAttribute('aria-checked')).toBe('true');
    expect(themeCard(en.settings.general.appearanceSystem).getAttribute('aria-checked')).toBe('false');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(window.localStorage.getItem('vibe-remote-theme')).toBe('light');

    // The OS flipping underneath must not move an explicitly chosen Light.
    flip(false);
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(themeCard(en.settings.general.appearanceLight).getAttribute('aria-checked')).toBe('true');
  });

  it('follows the system while System is selected, and returns to following it', async () => {
    const user = userEvent.setup();
    renderPage();

    // System writes no data-theme at all — the stylesheet's own media query
    // resolves it, so following the OS needs nothing recorded on the document.
    expect(document.documentElement.getAttribute('data-theme')).toBeNull();

    flip(true);
    expect(document.documentElement.getAttribute('data-theme')).toBeNull();
    expect(themeCard(en.settings.general.appearanceSystem).getAttribute('aria-checked')).toBe('true');

    await user.click(themeCard(en.settings.general.appearanceDark));
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

    await user.click(themeCard(en.settings.general.appearanceSystem));
    expect(document.documentElement.getAttribute('data-theme')).toBeNull();
    expect(window.localStorage.getItem('vibe-remote-theme')).toBe('system');
  });

  it('is one tab stop that arrows between the three choices', async () => {
    const user = userEvent.setup();
    renderPage();

    const system = themeCard(en.settings.general.appearanceSystem);
    expect(system.getAttribute('tabindex')).toBe('0');
    expect(themeCard(en.settings.general.appearanceLight).getAttribute('tabindex')).toBe('-1');

    system.focus();
    await user.keyboard('{ArrowRight}');
    expect(document.activeElement).toBe(themeCard(en.settings.general.appearanceLight));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');

    await user.keyboard('{ArrowRight}');
    expect(document.activeElement).toBe(themeCard(en.settings.general.appearanceDark));
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

    // Wraps, the way a radio group does.
    await user.keyboard('{ArrowRight}');
    expect(document.activeElement).toBe(themeCard(en.settings.general.appearanceSystem));
    expect(window.localStorage.getItem('vibe-remote-theme')).toBe('system');

    await user.keyboard('{End}');
    expect(document.activeElement).toBe(themeCard(en.settings.general.appearanceDark));
  });

  it('draws each theme rather than previewing the current one', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(themeCard(en.settings.general.appearanceDark));

    // Source swatches (Q8zxF1): the Light card stays light while the app is
    // dark, and System keeps both halves instead of collapsing into the
    // resolved theme — which is what made the source's System card unreadable.
    const lightShell = 'rgb(244, 246, 251)';
    const darkShell = 'rgb(8, 8, 18)';
    const light = themeCard(en.settings.general.appearanceLight);
    const system = themeCard(en.settings.general.appearanceSystem);
    const dark = themeCard(en.settings.general.appearanceDark);

    expect(light.querySelectorAll(`[style*="${lightShell}"]`).length).toBeGreaterThan(0);
    expect(light.querySelectorAll(`[style*="${darkShell}"]`).length).toBe(0);
    expect(dark.querySelectorAll(`[style*="${darkShell}"]`).length).toBeGreaterThan(0);
    expect(dark.querySelectorAll(`[style*="${lightShell}"]`).length).toBe(0);
    expect(system.querySelectorAll(`[style*="${lightShell}"]`).length).toBeGreaterThan(0);
    expect(system.querySelectorAll(`[style*="${darkShell}"]`).length).toBeGreaterThan(0);

    // The drawings are decoration; the label is what names the control.
    for (const card of [light, system, dark]) {
      expect(card.querySelector('[aria-hidden="true"]')).toBeTruthy();
    }
  });
});
