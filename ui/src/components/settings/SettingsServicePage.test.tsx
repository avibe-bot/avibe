/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsServicePage } from './SettingsServicePage';

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
}));
const status = vi.hoisted(() => ({
  control: vi.fn(),
  value: { state: 'running', service_pid: 123 },
}));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/StatusContext', () => ({
  useStatus: () => ({ status: status.value, control: status.control }),
}));
vi.mock('@/context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));
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
    ui: { setup_host: '127.0.0.1', setup_port: 5123 },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SettingsServicePage lifecycle contention', () => {
  it.each([
    ['restart_in_progress', 'settings.service.restartInProgress'],
    ['restart_not_scheduled_package_busy', 'settings.service.packageMutationBusy'],
  ])('shows a localized error toast for %s', async (code, message) => {
    status.control.mockRejectedValue(Object.assign(new Error(code), { code }));
    renderPage();

    await userEvent.click(screen.getByRole('button', { name: 'common.restart' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith(message, 'error'));
  });

  it('keeps unexpected control failures on the existing logging path', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    status.control.mockRejectedValue(new Error('unexpected'));
    renderPage();

    await userEvent.click(screen.getByRole('button', { name: 'common.restart' }));

    await waitFor(() => expect(consoleError).toHaveBeenCalled());
    expect(showToast).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});
