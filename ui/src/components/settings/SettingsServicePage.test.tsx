/* @vitest-environment jsdom */
/* @vitest-environment-options {"url": "http://127.0.0.1:5123/settings/service"} */

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('@/lib/apiFetch', () => ({ apiFetch }));
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

describe('the console server host on an Avibe bound to an account', () => {
  it.each([
    ['bound', { instance_id: 'fixture-instance' }, true],
    ['not bound', { instance_id: '' }, false],
  ])('%s: says whether a network address still lets other devices in', async (_label, vibe_cloud, said) => {
    api.getConfig.mockResolvedValue({ ui: { setup_host: '192.168.1.20' }, remote_access: { vibe_cloud } });
    renderPage();

    expect(await screen.findByText(en.settings.consoleServerHint)).toBeTruthy();
    expect(Boolean(screen.queryByText(en.settings.consoleServerBound))).toBe(said);
  });
});

describe('saving the console server host', () => {
  const saveHost = async (host: string) => {
    api.getConfig.mockResolvedValue({ ui: { setup_host: '127.0.0.1', setup_port: 5123 } });
    api.mutateConfig.mockResolvedValue({});
    renderPage();
    const field = await screen.findByLabelText(en.dashboard.host);
    // One edit: the field puts its default back when emptied, so it cannot be cleared and retyped.
    fireEvent.change(field, { target: { value: host } });
    await userEvent.click(screen.getByRole('button', { name: en.common.saveAndRestart }));
  };
  const hostsWritten = () => api.mutateConfig.mock.calls.map(([changes]) => changes[0].value);

  it('writes nothing for an address the server cannot listen on, and says so', async () => {
    await saveHost('192.168.1');

    expect(await screen.findByText(en.settings.consoleServerInvalidHost)).toBeTruthy();
    expect(api.mutateConfig).not.toHaveBeenCalled();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it('takes the host back out of the configuration when the server refuses to listen there', async () => {
    apiFetch.mockResolvedValue({ ok: false, status: 400 });
    await saveHost('192.168.1.20');

    expect(await screen.findByText(en.common.saveFailed)).toBeTruthy();
    expect(hostsWritten()).toEqual(['192.168.1.20', '127.0.0.1']);
  });

  it('keeps a page on this computer where it is when the host becomes a network address', async () => {
    apiFetch.mockResolvedValue({ ok: true, status: 200 });
    await saveHost('192.168.1.20');

    expect(await screen.findByText(en.dashboard.uiRestartMessage)).toBeTruthy();
    expect(hostsWritten()).toEqual(['192.168.1.20']);
    expect(JSON.parse(apiFetch.mock.calls[0][1].body)).toEqual({ host: '192.168.1.20', port: 5123 });
  });
});
