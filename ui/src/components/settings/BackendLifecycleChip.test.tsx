/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BackendLifecycleChip } from './BackendLifecycleChip';

const api = vi.hoisted(() => ({
  getBackendRuntime: vi.fn(),
  installAgent: vi.fn(),
  restartBackend: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('@/context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('@/context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('@/context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const updateAvailable = {
  ok: true,
  name: 'codex',
  enabled: true,
  cli_path: '/usr/local/bin/codex',
  resolved_path: '/usr/local/bin/codex',
  installed: true,
  current_version: '1.0.0',
  latest_version: '1.1.0',
  has_update: true,
  supports_restart: true,
  process_status: 'running',
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
};

beforeEach(() => {
  api.getBackendRuntime.mockResolvedValue(updateAvailable);
  api.installAgent.mockResolvedValue({ ok: true, message: '', output: null });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('BackendLifecycleChip', () => {
  it('keeps an active upgrade visible across popover dismissal and stale runtime refreshes', async () => {
    const install = deferred<{ ok: boolean; message: string; output: null }>();
    api.installAgent.mockReturnValue(install.promise);
    const user = userEvent.setup();

    render(<BackendLifecycleChip name="codex" enabled cliStatus="ok" />);

    const chip = await screen.findByRole('button', {
      name: 'backendLifecycle.statusUpdateAvailable',
    });
    await user.click(chip);
    await user.click(await screen.findByRole('button', { name: 'backendLifecycle.upgradeNow' }));
    expect(chip.getAttribute('aria-label')).toBe('backendLifecycle.statusUpdating');

    const probesBeforeDismiss = api.getBackendRuntime.mock.calls.length;
    await user.click(document.body);
    expect(screen.queryByText('backendLifecycle.title')).toBeNull();
    expect(chip.getAttribute('aria-label')).toBe('backendLifecycle.statusUpdating');
    expect(api.getBackendRuntime).toHaveBeenCalledTimes(probesBeforeDismiss);

    const probesBeforeReopen = api.getBackendRuntime.mock.calls.length;
    await user.click(chip);
    await waitFor(() => {
      expect(api.getBackendRuntime.mock.calls.length).toBeGreaterThan(probesBeforeReopen);
    });
    expect(await screen.findByText('backendLifecycle.upgrading')).toBeTruthy();
    expect(chip.getAttribute('aria-label')).toBe('backendLifecycle.statusUpdating');
    expect(screen.queryByRole('button', { name: 'backendLifecycle.upgradeNow' })).toBeNull();

    install.resolve({ ok: true, message: '', output: null });
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'backendLifecycle.upgradeSuccess',
      'success',
    ));
    await waitFor(() => expect(chip.getAttribute('aria-label')).toBe(
      'backendLifecycle.statusUpdateAvailable',
    ));
  });
});
