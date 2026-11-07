/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '../context/ToastProvider';
import { LanguageSwitcher } from './LanguageSwitcher';

const api = vi.hoisted(() => ({ getConfig: vi.fn(), mutateConfig: vi.fn() }));
const authorization = vi.hoisted(() => ({
  capabilities: { can_manage_instance: true },
}));
const i18n = vi.hoisted(() => ({
  language: 'en',
  // The Web UI's instance holds only the language it loaded (src/i18n/app.ts).
  options: { resources: { en: {} } },
  loadLanguages: vi.fn(async () => undefined),
  changeLanguage: vi.fn(),
}));

vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n }),
}));

// The real provider, not a stub: a failed instance save is reported through it,
// and every surface that mounts this switcher sits inside one (App nests
// ToastProvider above the router, which covers the wizard chrome too).
const renderSwitcher = () => render(
  <ToastProvider>
    <LanguageSwitcher />
  </ToastProvider>,
);

beforeEach(() => {
  authorization.capabilities.can_manage_instance = true;
  i18n.language = 'en';
  i18n.changeLanguage.mockResolvedValue(undefined);
  api.getConfig.mockResolvedValue({ language: 'en' });
  api.mutateConfig.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('LanguageSwitcher role-aware persistence', () => {
  it('keeps a member browser preference without reading or writing the instance default', async () => {
    authorization.capabilities.can_manage_instance = false;
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(screen.getByRole('button', { name: 'language.en' }));
    await user.click(screen.getByRole('option', { name: 'language.zh' }));

    expect(i18n.changeLanguage).toHaveBeenCalledWith('zh');
    expect(api.getConfig).not.toHaveBeenCalled();
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });

  it('persists an owner language change to the instance config', async () => {
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(screen.getByRole('button', { name: 'language.en' }));
    await user.click(screen.getByRole('option', { name: 'language.zh' }));

    expect(i18n.changeLanguage).toHaveBeenCalledWith('zh');
    expect(api.mutateConfig).toHaveBeenCalledOnce();
  });
});

// The Web UI downloads one language's copy at a time and switches once it has
// arrived (src/i18n/app.ts); until then the language on screen is the old one.
describe('LanguageSwitcher with languages downloaded on demand', () => {
  it('readies every language it offers when it appears', () => {
    renderSwitcher();

    expect(i18n.loadLanguages).toHaveBeenCalledWith(['en', 'zh']);
  });

  it('lets the reader go back while the language they picked is still on its way', async () => {
    let arrive!: () => void;
    i18n.changeLanguage.mockImplementationOnce(() => new Promise<void>((resolve) => { arrive = resolve; }));
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(screen.getByRole('button', { name: 'language.en' }));
    await user.click(screen.getByRole('option', { name: 'language.zh' }));
    await user.click(screen.getByRole('button', { name: 'language.en' }));
    await user.click(screen.getByRole('option', { name: 'language.en' }));

    expect(i18n.changeLanguage).toHaveBeenLastCalledWith('en');
    await waitFor(() => expect(api.mutateConfig).toHaveBeenCalledTimes(2));
    expect(api.mutateConfig).toHaveBeenLastCalledWith([{ kind: 'set', path: ['language'], value: 'en' }]);
    arrive();
  });
});
