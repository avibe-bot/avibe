/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsDependenciesPage } from './SettingsDependenciesPage';

const api = vi.hoisted(() => ({
  getMemoryStatus: vi.fn(),
  installDependency: vi.fn(),
  listDependencies: vi.fn(),
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
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => (
      values?.version ? `${key}:${String(values.version)}` : key
    ),
  }),
}));

vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => (
    <>{actions}{children}</>
  ),
}));

const dependency = (overrides = {}) => ({
  id: 'memory-runtime',
  kind: 'runtime' as const,
  required: false,
  installed: true,
  status: 'ready' as const,
  version: '1.0.0',
  ...overrides,
});

const renderPage = () => render(
  <MemoryRouter>
    <SettingsDependenciesPage />
  </MemoryRouter>,
);

beforeEach(() => {
  api.listDependencies.mockResolvedValue({ ok: true, deps: [dependency()] });
  api.getMemoryStatus.mockResolvedValue({
    status: 'ok',
    state: 'disabled',
    reason: null,
    source: { status: 'unavailable', observed_at: null, reason: 'memory_disabled' },
    health: null,
  });
  api.installDependency.mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SettingsDependenciesPage Show Runtime status', () => {
  it('renders inspection failure without authorizing install or repair', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [dependency({
        id: 'show-runtime',
        installed: null,
        status: 'error',
        action_class: 'operator_only',
        version: null,
        reason: 'runtime_install_inspection_failed',
      })],
    });

    renderPage();

    expect(await screen.findByText('settings.dependencies.statusError')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toBe('errors.runtime_install_inspection_failed');
    expect(screen.queryByRole('button', { name: 'settings.dependencies.install' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'settings.dependencies.repair' })).toBeNull();
    expect(api.installDependency).not.toHaveBeenCalled();
  });

  it('keeps a proven absence installable', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [dependency({
        id: 'show-runtime',
        installed: false,
        status: 'missing',
        action_class: 'repairable',
        version: null,
      })],
    });

    renderPage();
    await userEvent.click(await screen.findByRole('button', { name: 'settings.dependencies.install' }));

    await waitFor(() => expect(api.installDependency).toHaveBeenCalledWith('show-runtime'));
  });
});

describe('SettingsDependenciesPage Model Hub engine', () => {
  it('shows the installed and pinned CPA versions and offers the update action', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [dependency({
        id: 'model-hub-engine',
        required: true,
        installed: true,
        version: 'v7.2.105',
        latest_version: 'v7.2.149',
        has_update: true,
        status: 'upgrade_required',
        action_class: 'repairable',
      })],
    });

    renderPage();

    expect(await screen.findByText('settings.dependencies.targetVersion:7.2.149')).toBeTruthy();
    expect(screen.getByText('settings.dependencies.statusUpgradeRequired · v7.2.105')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'settings.dependencies.update' }));
    await waitFor(() => expect(api.installDependency).toHaveBeenCalledWith('model-hub-engine'));
  });
});

describe('SettingsDependenciesPage Memory runtime', () => {
  it('routes repairable Python package bootstrap through its own dependency action', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [dependency({
        id: 'memory-package',
        installed: false,
        status: 'missing',
        action_class: 'repairable',
        version: null,
      })],
    });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: 'settings.dependencies.install' }));

    await waitFor(() => expect(api.installDependency).toHaveBeenCalledWith('memory-package'));
    expect(api.installDependency).not.toHaveBeenCalledWith('memory-runtime');
  });

  it('hides package bootstrap while Memory is not required', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [dependency({
        id: 'memory-package',
        installed: false,
        status: 'not_required',
        action_class: 'none',
        version: null,
      })],
    });
    renderPage();

    expect(await screen.findByText('settings.dependencies.statusNotRequired')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'settings.dependencies.install' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'settings.dependencies.repair' })).toBeNull();
  });

  it('keeps explicit package repair available while disabled', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [dependency({
        id: 'memory-package',
        status: 'not_required',
        action_class: 'repairable',
      })],
    });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: 'settings.dependencies.reinstall' }));

    await waitFor(() => expect(api.installDependency).toHaveBeenCalledWith('memory-package'));
  });

  it('shows only dependency repair and the Memory settings link', async () => {
    renderPage();
    expect(await screen.findByRole('button', { name: 'settings.dependencies.repair' })).toBeTruthy();
    expect(screen.getByRole('link', { name: /common.configure/ }).getAttribute('href')).toBe(
      '/settings/memory',
    );
    expect(screen.queryByText(/Factory Reset|Reinitialize Memory|memory\.factoryReset/)).toBeNull();
  });

  it.each(['starting', 'running', 'degraded'] as const)(
    'blocks artifact replacement while runtime state is %s',
    async (state) => {
      api.getMemoryStatus.mockResolvedValue({
        status: 'ok',
        state,
        reason: state === 'degraded' ? 'memory_provider_timeout' : null,
        source: { status: 'unavailable', observed_at: null, reason: null },
        health: null,
      });
      renderPage();
      const repair = await screen.findByRole('button', { name: 'settings.dependencies.repair' });
      expect((repair as HTMLButtonElement).disabled).toBe(true);
      expect(screen.getByText('settings.dependencies.memoryRuntimeDisableBeforeRepair')).toBeTruthy();
    },
  );

  it('repairs the admitted artifact without invoking a Memory data operation', async () => {
    renderPage();
    await userEvent.click(await screen.findByRole('button', { name: 'settings.dependencies.repair' }));
    await waitFor(() => expect(api.installDependency).toHaveBeenCalledWith('memory-runtime'));
    expect(Object.keys(api).sort()).toEqual([
      'getMemoryStatus',
      'installDependency',
      'listDependencies',
    ]);
  });

  it('renders a persisted preparation reason on initial page load', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [dependency({
        installed: false,
        status: 'error',
        version: null,
        reason: 'memory_runtime_preparation_import_timeout',
      })],
    });

    renderPage();

    const failure = await screen.findByRole('alert');
    expect(failure.textContent).toBe('errors.memory_runtime_preparation_import_timeout');
    expect(screen.getByText('settings.dependencies.statusError')).toBeTruthy();
    expect(api.installDependency).not.toHaveBeenCalled();
  });

  it.each([
    'memory_runtime_preparation_failed',
    'memory_runtime_preparation_import_timeout',
    'memory_runtime_preparation_import_failed',
    'memory_runtime_preparation_scrubber_timeout',
    'memory_runtime_preparation_scrubber_failed',
    'memory_runtime_preparation_sync_contract_failed',
  ])('localizes the bounded preparation reason %s', async (reason) => {
    api.installDependency.mockResolvedValue({ ok: false, reason });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: 'settings.dependencies.repair' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith(`errors.${reason}`, 'error'));
  });
});
