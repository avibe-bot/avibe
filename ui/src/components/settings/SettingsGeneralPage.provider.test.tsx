/* @vitest-environment jsdom */

import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { createInstance, type i18n as I18n } from 'i18next';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
// The wire is the only stand-in in this file. Everything above it is the app's
// own code — the provider that memoizes its value on `t`, react-i18next's
// instance copying, the real page — which is what separates this from the
// sibling file's mocked api: there the identity rule is reproduced, here it is
// the one the product actually has.
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));

const authorization = vi.hoisted(() => ({ capabilities: { can_manage_instance: true } }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));

vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      media: query,
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
});

import { ApiProvider, useApi } from '../../context/ApiContext';
import { ThemeProvider } from '../../context/ThemeProvider';
import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { SettingsGeneralPage } from './SettingsGeneralPage';

// One instance per test: the language operation belongs to the i18n instance and
// outlives any render root, so a shared one would hand this test's pending save
// to the next.
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

const json = (payload: unknown) => new Response(JSON.stringify(payload), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
});

/** A config endpoint with a memory and the test's hand on its clock: a write is
 *  applied to it when the test lets that request answer, not when the app sends
 *  it, so what the instance ends up with is decided by the order the writes land
 *  in. */
const instanceServer = () => {
  const stored: Record<string, unknown> = { language: 'en' };
  const inFlight: { answer: () => void }[] = [];
  apiFetch.mockImplementation((path: string, init?: RequestInit) => {
    if (path !== '/api/config' || init?.method !== 'POST') {
      // Reads answer with the instance as it stands; anything else this app asks
      // on mount gets an empty, successful answer, because this test speaks only
      // about config.
      return Promise.resolve(json(path === '/api/config' ? stored : {}));
    }
    const payload = JSON.parse(String(init.body));
    return new Promise<Response>((resolve) => {
      inFlight.push({
        answer: () => {
          Object.assign(stored, payload);
          resolve(json({ ...stored }));
        },
      });
    });
  });
  return {
    stored,
    inFlight,
    /** The language writes the app has actually put on the wire, in order. */
    sent: () => apiFetch.mock.calls
      .filter(([path, init]) => path === '/api/config' && init?.method === 'POST')
      .map(([, init]) => JSON.parse(String(init.body)).language),
  };
};

/** Every distinct api the provider has handed a consumer. A language change is
 *  itself what produces a new one, which is the churn this file exists to put
 *  the real provider behind. */
const handedOut: unknown[] = [];
const WatchApi = () => {
  const api = useApi();
  useEffect(() => {
    handedOut.push(api);
  }, [api]);
  return null;
};

const App = ({ instance, children }: { instance: I18n; children: ReactNode }) => (
  <I18nextProvider i18n={instance}>
    <ThemeProvider>
      <ToastProvider>
        <ApiProvider>
          <WatchApi />
          {children}
        </ApiProvider>
      </ToastProvider>
    </ThemeProvider>
  </I18nextProvider>
);

const renderApp = () => {
  const at = (children: ReactNode) => <App instance={i18n}>{children}</App>;
  const view = render(at(<SettingsGeneralPage />));
  return {
    ...view,
    /** Leaves Settings the way the app does: the page unmounts, the rest stays. */
    leave: () => view.rerender(at(<div />)),
    comeBack: () => view.rerender(at(<SettingsGeneralPage />)),
  };
};

const languageSelect = () => screen.getByRole('combobox') as HTMLSelectElement;

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  authorization.capabilities.can_manage_instance = true;
  apiFetch.mockReset();
  handedOut.length = 0;
  i18n = makeI18n();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('General settings — language through the real ApiProvider', () => {
  it('holds one pending instance save in order across the locale, the api handed out, and a remount', async () => {
    const server = instanceServer();
    const user = userEvent.setup();
    const view = renderApp();
    const beforeAnyPick = new Set(handedOut).size;

    await user.selectOptions(languageSelect(), 'zh');
    await waitFor(() => expect(screen.getByText(zh.settings.general.title)).toBeTruthy());

    // The write is on the wire and unanswered, and the interface is already
    // Chinese — so the provider has re-memoized and the page is holding an api
    // it did not have when the pick was made.
    expect(server.sent()).toEqual(['zh']);
    expect(new Set(handedOut).size).toBeGreaterThan(beforeAnyPick);

    view.leave();
    view.comeBack();

    await user.selectOptions(languageSelect(), 'en');
    await waitFor(() => expect(screen.getByText(en.settings.general.title)).toBeTruthy());

    // Queued behind the first, not sent beside it: two writes in flight would
    // let the older one land last and leave the instance on a language the user
    // has already moved off.
    expect(server.sent()).toEqual(['zh']);

    await act(async () => { server.inFlight[0].answer(); });
    await waitFor(() => expect(server.sent()).toEqual(['zh', 'en']));
    await act(async () => { server.inFlight[1].answer(); });

    await waitFor(() => expect(server.stored.language).toBe('en'));
    expect(languageSelect().value).toBe('en');
  });
});
