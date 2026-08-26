/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LanguageSwitcher } from './LanguageSwitcher';

const api = vi.hoisted(() => ({ getConfig: vi.fn(), mutateConfig: vi.fn() }));
const authorization = vi.hoisted(() => ({
  capabilities: { can_manage_instance: true },
}));
const i18n = vi.hoisted(() => ({
  language: 'en',
  options: { resources: { en: {}, zh: {} } },
  changeLanguage: vi.fn(),
}));

vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n }),
}));

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
    render(<LanguageSwitcher />);

    await user.click(screen.getByRole('button', { name: 'language.en' }));
    await user.click(screen.getByRole('option', { name: 'language.zh' }));

    expect(i18n.changeLanguage).toHaveBeenCalledWith('zh');
    expect(api.getConfig).not.toHaveBeenCalled();
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });

  it('persists an owner language change to the instance config', async () => {
    const user = userEvent.setup();
    render(<LanguageSwitcher />);

    await user.click(screen.getByRole('button', { name: 'language.en' }));
    await user.click(screen.getByRole('option', { name: 'language.zh' }));

    expect(i18n.changeLanguage).toHaveBeenCalledWith('zh');
    expect(api.mutateConfig).toHaveBeenCalledOnce();
  });
});
