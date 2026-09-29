/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ getConfig: vi.fn(), mutateConfig: vi.fn() }));
const statusContext = vi.hoisted(() => ({
  status: { state: 'running', service_pid: 4321 },
  health: true,
  refreshStatus: vi.fn(),
  control: vi.fn(),
}));

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../context/StatusContext', () => ({ useStatus: () => statusContext }));
vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({ children }: { children: React.ReactNode }) => children,
}));

import en from '../../i18n/en.json';
import { SettingsServicePage } from './SettingsServicePage';

const renderPage = () => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: 'en',
    resources: { en: { translation: en } },
    interpolation: { escapeValue: false },
  });
  return render(
    <I18nextProvider i18n={i18n}>
      <MemoryRouter>
        <SettingsServicePage />
      </MemoryRouter>
    </I18nextProvider>,
  );
};

afterEach(() => {
  cleanup();
  statusContext.status = { state: 'running', service_pid: 4321 };
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe('service control refused by the server', () => {
  it('tells the person the restart did not happen and why', async () => {
    api.getConfig.mockResolvedValue({ ui: {} });
    // What StatusProvider.control throws for the 409 /api/control answers when
    // the Avibe running here is not this desktop Runtime's.
    statusContext.control.mockRejectedValue(
      Object.assign(new Error('Control action restart failed with status 409'), { code: 'restart_refused' }),
    );
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderPage();

    const restart = screen.getByRole('button', { name: en.common.restart });
    await userEvent.click(restart);

    expect(statusContext.control).toHaveBeenCalledWith('restart');
    expect(await screen.findByText(en.settings.restartRefused)).toBeTruthy();
    expect(screen.queryByText('PID 4321')).toBeNull();
    expect((restart as HTMLButtonElement).disabled).toBe(false);
  });

  it('tells the person the start did not happen and why', async () => {
    api.getConfig.mockResolvedValue({ ui: {} });
    statusContext.status = { state: 'stopped', service_pid: 4321 };
    // What StatusProvider.control throws for the 409 /api/control answers when
    // the service running here is not this desktop Runtime's.
    statusContext.control.mockRejectedValue(
      Object.assign(new Error('Control action start failed with status 409'), { code: 'start_refused' }),
    );
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderPage();

    const start = screen.getByRole('button', { name: en.common.start });
    await userEvent.click(start);

    expect(statusContext.control).toHaveBeenCalledWith('start');
    expect(await screen.findByText(en.settings.startRefused)).toBeTruthy();
    expect((start as HTMLButtonElement).disabled).toBe(false);
  });
});
