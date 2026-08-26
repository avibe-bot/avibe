/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsServicePage } from './SettingsServicePage';

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  mutateConfig: vi.fn(),
}));
const status = vi.hoisted(() => ({
  control: vi.fn(),
  value: { state: 'running', service_pid: 123 },
}));
const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/StatusContext', () => ({
  useStatus: () => ({ status: status.value, control: status.control }),
}));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('@/lib/apiFetch', () => ({ apiFetch }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const renderPage = () => render(
  <MemoryRouter>
    <SettingsServicePage />
  </MemoryRouter>,
);

beforeEach(() => {
  api.getConfig.mockResolvedValue({
    ui: { setup_host: '127.0.0.2', setup_port: 6000 },
  });
  api.mutateConfig.mockResolvedValue({});
  apiFetch.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('SettingsServicePage UI reload admission', () => {
  it.each([
    ['restart_in_progress', 'settings.service.restartInProgress'],
    ['restart_not_scheduled_package_busy', 'settings.service.packageMutationBusy'],
  ])('surfaces %s without redirecting', async (code, message) => {
    apiFetch.mockResolvedValue(
      new Response(JSON.stringify({ ok: false, code, error: 'busy' }), { status: 409 }),
    );
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout');
    renderPage();

    await waitFor(() => expect(screen.getByRole('button', { name: 'common.saveAndRestart' })).toBeTruthy());
    await userEvent.click(screen.getByRole('button', { name: 'common.saveAndRestart' }));

    await waitFor(() => expect(screen.getByText(message)).toBeTruthy());
    expect(setTimeoutSpy.mock.calls.some(([, delay]) => delay === 1500)).toBe(false);
  });

  it('keeps the success redirect after an admitted reload', async () => {
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout');
    renderPage();

    await waitFor(() => expect(screen.getByRole('button', { name: 'common.saveAndRestart' })).toBeTruthy());
    await userEvent.click(screen.getByRole('button', { name: 'common.saveAndRestart' }));
    await waitFor(() => expect(screen.getByText(/settings\.consoleServerRedirecting/)).toBeTruthy());

    expect(setTimeoutSpy).toHaveBeenCalledWith(expect.any(Function), 1500);
  });
});
