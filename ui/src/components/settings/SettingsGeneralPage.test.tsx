/* @vitest-environment jsdom */

import { createInstance, type i18n as I18n } from 'i18next';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ mutateConfig: vi.fn() }));
const apiHandouts = vi.hoisted(() => [] as { t: unknown; value: unknown }[]);
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

// The real ApiContext memoizes its value on `t`, so a language change is itself
// what hands every consumer a new `mutateConfig`. Reproduced here — through the
// real `t`, not a stand-in — because that identity is the one a language queue
// must not be keyed on: keying on it would rebuild the queue on the very
// operation it is meant to order.
vi.mock('../../context/ApiContext', async () => {
  const { useMemo } = await import('react');
  const { useTranslation } = await import('react-i18next');
  return {
    useApi: () => {
      const { t } = useTranslation();
      return useMemo(() => {
        const value = { mutateConfig: (...args: unknown[]) => api.mutateConfig(...args) };
        apiHandouts.push({ t, value });
        return value;
      }, [t]);
    },
  };
});
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));

import { ThemeProvider } from '../../context/ThemeProvider';
import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { LanguageSwitcher } from '../LanguageSwitcher';
import { SettingsGeneralPage } from './SettingsGeneralPage';

// One per test, not one per file: the language operation is owned by the i18n
// instance and outlives any render root, so a shared instance would carry one
// test's pending save into the next. Two instances in the same test are how the
// isolation between interfaces gets proved.
const makeI18n = () => {
  const instance = createInstance();
  void instance.use(initReactI18next).init({
    lng: 'en',
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  return instance;
};

let i18n = makeI18n();

/** Everything above the page: what the app keeps mounted while the user walks
 *  in and out of Settings, so a toast and a save can outlive the page. */
const Shell = ({ instance, children }: { instance: I18n; children: ReactNode }) => (
  <I18nextProvider i18n={instance}>
    <ThemeProvider>
      <ToastProvider>{children}</ToastProvider>
    </ThemeProvider>
  </I18nextProvider>
);

const renderPage = (instance: I18n = i18n) => {
  const show = () => view.rerender(<Shell instance={instance}><SettingsGeneralPage /></Shell>);
  const view = render(<Shell instance={instance}><SettingsGeneralPage /></Shell>);
  return {
    ...view,
    /** Leaves Settings the way the app does: the page unmounts, the shell stays. */
    leave: () => view.rerender(<Shell instance={instance}><div /></Shell>),
    comeBack: show,
    /** The same re-render without having left: how anything above the page,
     *  a capability among them, reaches a control that is already on screen. */
    refresh: show,
  };
};

/** A capability change arrives at a control by re-rendering it, so these tests
 *  never flip one without the other — otherwise a control would be asked about
 *  an authority it has not been told about yet. */
const authorityBecomes = (canManage: boolean, refresh: () => void) => {
  authorization.capabilities.can_manage_instance = canManage;
  refresh();
};

const themeCard = (name: string) => screen.getByRole('radio', { name: new RegExp(name) });
// Located by role, not by label: the label is itself translated the moment the
// pick lands, which is part of what these tests are checking.
const languageSelect = () => screen.getByRole('combobox') as HTMLSelectElement;
const flip = (prefersLight: boolean) => act(() => {
  media.prefersLight = prefersLight;
  media.listeners.forEach((listener) => listener());
});

/** The instance as the server keeps it: whatever write landed last. */
const persisted = { language: 'en' };

/** Hands back control of when each save settles, so a race can be staged. A save
 *  reaches the instance when it settles, not when it is sent, which is the whole
 *  question these races ask. */
const deferSaves = () => {
  const pending: { value: unknown; resolve: () => void; reject: () => void }[] = [];
  api.mutateConfig.mockImplementation((mutations: { value: unknown }[]) => new Promise<void>((resolve, reject) => {
    const value = mutations[0]?.value;
    pending.push({
      value,
      resolve: () => { persisted.language = String(value); resolve(); },
      reject: () => reject(new Error('offline')),
    });
  }));
  return pending;
};

const saved = () => api.mutateConfig.mock.calls.map(([mutations]) => mutations[0].value);

beforeEach(async () => {
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  media.prefersLight = false;
  media.listeners.clear();
  authorization.capabilities.can_manage_instance = true;
  api.mutateConfig.mockReset();
  api.mutateConfig.mockResolvedValue(undefined);
  apiHandouts.length = 0;
  persisted.language = 'en';
  i18n = makeI18n();
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

  it('holds the order when the user leaves Settings and comes back mid-save', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    const view = renderPage();

    await user.selectOptions(languageSelect(), 'zh');
    expect(api.mutateConfig).toHaveBeenCalledTimes(1);

    // The control that started the save is gone before the instance answers,
    // and the one that replaces it is a different component with a different
    // set of refs. The save is not.
    view.leave();
    view.comeBack();
    await user.selectOptions(languageSelect(), 'en');

    expect(api.mutateConfig).toHaveBeenCalledTimes(1);
    await act(async () => { saves[0].resolve(); });
    await waitFor(() => expect(api.mutateConfig).toHaveBeenCalledTimes(2));
    await act(async () => { saves[1].resolve(); });

    expect(saved()).toEqual(['zh', 'en']);
    expect(persisted.language).toBe('en');
    expect(languageSelect().value).toBe('en');
  });

  it('holds the order between the settings selector and the compact switcher', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    render(
      <Shell instance={i18n}>
        <SettingsGeneralPage />
        <LanguageSwitcher />
      </Shell>,
    );

    await user.selectOptions(languageSelect(), 'zh');
    expect(api.mutateConfig).toHaveBeenCalledTimes(1);

    // Scoped to the switcher's own list: the page's selector offers the same two
    // languages, which is the point — two controls, one language.
    await user.click(screen.getByRole('button', { name: zh.language.zh }));
    await user.click(within(screen.getByRole('listbox')).getByRole('option', { name: zh.language.en }));

    // Two controls, one language: the second pick queues behind the first save
    // instead of racing it.
    expect(api.mutateConfig).toHaveBeenCalledTimes(1);
    await act(async () => { saves[0].resolve(); });
    await waitFor(() => expect(api.mutateConfig).toHaveBeenCalledTimes(2));
    await act(async () => { saves[1].resolve(); });

    expect(saved()).toEqual(['zh', 'en']);
    expect(persisted.language).toBe('en');
  });

  it('cannot retry a failure the user has already moved past', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    const view = renderPage();

    await user.selectOptions(languageSelect(), 'zh');
    await act(async () => { saves[0].reject(); });
    expect(await screen.findByText(zh.settings.general.languageSaveFailed)).toBeTruthy();

    // They pick something else instead of retrying, and that one saves — from
    // another page, so the failure's own control is gone as well.
    view.leave();
    view.comeBack();
    await user.selectOptions(languageSelect(), 'en');
    await act(async () => { saves[1].resolve(); });

    await user.click(screen.getByRole('button', { name: zh.common.retry }));
    await act(async () => {});

    expect(saved()).toEqual(['zh', 'en']);
    expect(persisted.language).toBe('en');
    expect(languageSelect().value).toBe('en');
    expect(screen.getByText(en.settings.general.title)).toBeTruthy();
  });

  it('cannot retry a failure whose language the user has come back to by another pick', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    renderPage();

    await user.selectOptions(languageSelect(), 'zh');
    await act(async () => { saves[0].reject(); });
    expect(await screen.findByText(zh.settings.general.languageSaveFailed)).toBeTruthy();

    await user.selectOptions(languageSelect(), 'en');
    await act(async () => { saves[1].resolve(); });
    await user.selectOptions(languageSelect(), 'zh');
    await act(async () => { saves[2].resolve(); });

    // The interface is back on the language that failed, but not because of that
    // failure: a later pick already saved it. Retrying the old one would be a
    // fourth write on behalf of a decision that has been superseded twice.
    await user.click(screen.getByRole('button', { name: zh.common.retry }));
    await act(async () => {});

    expect(saved()).toEqual(['zh', 'en', 'zh']);
    expect(persisted.language).toBe('zh');
  });

  it('does not retry a failed save from a toast that outlived every control', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    const view = renderPage();

    await user.selectOptions(languageSelect(), 'zh');
    await act(async () => { saves[0].reject(); });
    expect(await screen.findByText(zh.settings.general.languageSaveFailed)).toBeTruthy();

    // The toast lives above Settings, so it is still there — and still offering
    // Retry — after the page that raised it is gone.
    view.leave();
    await user.click(screen.getByRole('button', { name: zh.common.retry }));
    await act(async () => {});

    // Nothing on screen can answer for the user now, so nothing is written on
    // their behalf. The pick itself is untouched: it was never the save.
    expect(saved()).toEqual(['zh']);
    expect(persisted.language).toBe('en');
    view.comeBack();
    expect(languageSelect().value).toBe('zh');
  });

  it('does not spend authority on a queued pick that the user has since lost', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    const view = renderPage();

    // Both picks are made as a manager, so both are instance decisions and the
    // second is genuinely waiting in the queue when the authority goes.
    await user.selectOptions(languageSelect(), 'zh');
    await user.selectOptions(languageSelect(), 'en');
    expect(saved()).toEqual(['zh']);

    authorityBecomes(false, view.refresh);
    await act(async () => { saves[0].resolve(); });

    // A queued write reads the authority of the moment it runs, not of the
    // moment it was queued; the local pick still stands either way.
    expect(saved()).toEqual(['zh']);
    expect(persisted.language).toBe('zh');
    expect(languageSelect().value).toBe('en');
  });

  it('never turns a local-only pick into an instance write when the authority comes back', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    const view = renderPage();

    await user.selectOptions(languageSelect(), 'zh');
    expect(saved()).toEqual(['zh']);

    // Made as a member, this pick is a choice about their own interface. What it
    // is was settled when they made it.
    authorityBecomes(false, view.refresh);
    await user.selectOptions(languageSelect(), 'en');
    expect(saved()).toEqual(['zh']);

    // Authority returning is not a second pick. Nothing here asked for the
    // instance to be changed, so nothing may write it.
    authorityBecomes(true, view.refresh);
    await act(async () => { saves[0].resolve(); });

    expect(saved()).toEqual(['zh']);
    expect(persisted.language).toBe('zh');
    expect(languageSelect().value).toBe('en');
  });

  it('writes nothing when a Retry is pressed after the authority is gone', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    const view = renderPage();

    await user.selectOptions(languageSelect(), 'zh');
    await act(async () => { saves[0].reject(); });
    expect(await screen.findByText(zh.settings.general.languageSaveFailed)).toBeTruthy();

    authorityBecomes(false, view.refresh);
    await user.click(screen.getByRole('button', { name: zh.common.retry }));

    // The offer was made while they could still act on it; pressing it now is
    // not authority they still have.
    await act(async () => {});
    expect(saved()).toEqual(['zh']);
    expect(languageSelect().value).toBe('zh');
  });

  it('keeps two interfaces out of each other\'s queue', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    const first = renderPage();
    const second = renderPage(makeI18n());

    await user.selectOptions(within(first.container).getByRole('combobox'), 'zh');
    await user.selectOptions(within(second.container).getByRole('combobox'), 'zh');

    // Ordering is owned by the interface the language belongs to, so a second,
    // unrelated one is not made to wait behind it.
    expect(api.mutateConfig).toHaveBeenCalledTimes(2);
    await act(async () => { saves[0].resolve(); saves[1].resolve(); });
    expect(saved()).toEqual(['zh', 'zh']);
  });

  it('orders saves across the api identity the language change itself replaces', async () => {
    const saves = deferSaves();
    const user = userEvent.setup();
    renderPage();

    // One `t` so far. The real provider memoizes its value on it, so what this
    // asserts is that a language change is exactly what invalidates that memo.
    expect(new Set(apiHandouts.map((handout) => handout.t)).size).toBe(1);
    await user.selectOptions(languageSelect(), 'zh');
    expect(new Set(apiHandouts.map((handout) => handout.t)).size).toBe(2);

    await user.selectOptions(languageSelect(), 'en');
    expect(api.mutateConfig).toHaveBeenCalledTimes(1);
    await act(async () => { saves[0].resolve(); });
    await waitFor(() => expect(api.mutateConfig).toHaveBeenCalledTimes(2));
    await act(async () => { saves[1].resolve(); });

    expect(persisted.language).toBe('en');
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
