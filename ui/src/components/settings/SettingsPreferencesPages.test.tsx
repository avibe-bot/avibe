/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsAppearancePage } from './SettingsPreferencesPages';

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  mutateConfig: vi.fn(),
}));
const authorization = vi.hoisted(() => ({
  capabilities: { can_manage_instance: true },
}));
const i18n = vi.hoisted(() => ({
  language: 'en',
  options: { resources: { en: {}, zh: {} } },
  changeLanguage: vi.fn(),
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));
vi.mock('@/context/ThemeContext', () => ({
  useTheme: () => ({ mode: 'system', setMode: vi.fn() }),
}));
vi.mock('@/lib/useAuthAccount', () => ({
  useAuthAccount: () => ({ email: null, signingOut: false, signOut: vi.fn() }),
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

describe('SettingsAppearancePage language preference', () => {
  it('keeps a member browser choice without loading or writing the instance default', async () => {
    authorization.capabilities.can_manage_instance = false;
    const user = userEvent.setup();
    render(<SettingsAppearancePage />);

    await user.click(screen.getByRole('button', { name: 'language.zh' }));

    expect(i18n.changeLanguage).toHaveBeenCalledWith('zh');
    expect(api.getConfig).not.toHaveBeenCalled();
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });

  it('loads the instance default for an owner who can persist it', async () => {
    api.getConfig.mockResolvedValue({ language: 'zh' });
    render(<SettingsAppearancePage />);

    await waitFor(() => expect(i18n.changeLanguage).toHaveBeenCalledWith('zh'));
  });
});
