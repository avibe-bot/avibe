/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ getConfig: vi.fn(), mutateConfig: vi.fn() }));

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_manage_access_members: true } }),
}));
vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock('./PlatformConfigEmbed', () => ({ PlatformConfigEmbed: () => null }));

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { SettingsPlatformsPage } from './SettingsPlatformsPage';

const renderPage = () => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: 'en',
    resources: { en: { translation: en } },
    interpolation: { escapeValue: false },
  });
  return render(
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <SettingsPlatformsPage />
      </ToastProvider>
    </I18nextProvider>,
  );
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('config save whose restart the server refused', () => {
  it('says the change was saved but the service was not restarted', async () => {
    // WeChat is runnable once configured, so checking its tile enables it at once.
    const config = { platforms: { enabled: [] }, wechat: {} };
    api.getConfig.mockResolvedValue(config);
    // The /api/config answer when the hot reload failed and the fallback
    // restart was refused because the Avibe running here is not this desktop
    // Runtime's.
    api.mutateConfig.mockResolvedValue({
      ...config,
      platforms: { enabled: ['wechat'] },
      platform_runtime: {
        ok: false,
        hot_reconciled: false,
        restart_scheduled: false,
        restart_code: 'restart_refused',
        restart_error: "restart refused: the running service is not this desktop Runtime's (runtime_id_mismatch)",
      },
    });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: /WeChat/ }));

    expect(api.mutateConfig).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(en.settings.configRestartRefused)).toBeTruthy();
    expect(screen.queryByText(en.platform.restartedSuccess)).toBeNull();
  });
});
