/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { SettingsDependenciesPage } from './SettingsDependenciesPage';

const api = vi.hoisted(() => ({
  factoryResetMemory: vi.fn(),
  getMemorySettings: vi.fn(),
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
    t: (key: string, options?: { returnObjects?: boolean; deleted?: string; label?: string; path?: string }) => {
      if (options?.returnObjects) return [`${key}.0`, `${key}.1`];
      if (options?.label && options?.deleted) return `${key}:${options.label}:${options.deleted}`;
      if (options?.path) return `${key}:${options.path}`;
      return options?.deleted ? `${key}:${options.deleted}` : key;
    },
  }),
}));

vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({
    actions,
    children,
  }: {
    actions?: React.ReactNode;
    children: React.ReactNode;
  }) => <>{actions}{children}</>,
}));

vi.mock('../ui/confirm-dialog', () => ({
  ConfirmDialog: ({
    open,
    destructive,
    holdSeconds,
    title,
    confirmLabel,
    confirmDisabled,
    onConfirm,
    children,
  }: {
    open: boolean;
    destructive?: boolean;
    holdSeconds?: number;
    title: string;
    confirmLabel?: string;
    confirmDisabled?: boolean;
    onConfirm: () => void | Promise<void>;
    children?: React.ReactNode;
  }) => open ? (
    <div
      data-testid="reinitialize-confirm"
      data-destructive={String(destructive)}
      data-hold-seconds={holdSeconds}
      data-title={title}
      data-confirm-label={confirmLabel}
    >
      {children}
      <button type="button" disabled={confirmDisabled} onClick={() => void onConfirm()}>
        confirm-reinitialize
      </button>
    </div>
  ) : null,
}));

const memoryRuntime = (overrides = {}) => ({
  id: 'memory-runtime',
  kind: 'runtime' as const,
  required: false,
  installed: true,
  status: 'ready' as const,
  version: '1.0.0',
  ...overrides,
});

const settings = (factoryResetRequired = false) => ({
  status: 'ok' as const,
  enabled: true,
  factory_reset_required: factoryResetRequired,
  processing: {
    llm: { base_url: null, model: null, api_key: null, has_api_key: false },
    embedding: { base_url: null, model: null, api_key: null, has_api_key: false },
  },
});

const renderPage = () => render(
  <MemoryRouter>
    <SettingsDependenciesPage />
  </MemoryRouter>,
);

beforeEach(() => {
  api.listDependencies.mockResolvedValue({ ok: true, deps: [memoryRuntime()] });
  api.getMemorySettings.mockResolvedValue(settings());
  api.getMemoryStatus.mockResolvedValue({ status: 'failed', error: 'memory_sidecar_unavailable' });
  api.installDependency.mockResolvedValue({ ok: true });
  api.factoryResetMemory.mockResolvedValue({
    ok: true,
    result: 'completed',
    data_deleted: true,
    data_remaining: false,
    roots: [
      { path: 'memory', existed: true, deleted: true },
      { path: 'state/memory', existed: true, deleted: true },
    ],
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('SettingsDependenciesPage Memory reinitialization', () => {
  it('MEMORY-FACTORY-101 requires installed, ready artifact and known public Memory settings', async () => {
    api.getMemorySettings.mockRejectedValue(new Error('forbidden'));
    renderPage();

    const action = await screen.findByRole('button', { name: 'memory.factoryReset.button' });
    expect((action as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('memory.factoryReset.settingsUnavailable')).toBeTruthy();
  });

  it('refreshes Memory settings when Re-check All retries a failed settings request', async () => {
    api.getMemorySettings
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValue(settings());
    const user = userEvent.setup();
    renderPage();

    const action = await screen.findByRole('button', { name: 'memory.factoryReset.button' });
    expect((action as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole('button', { name: 'settings.dependencies.recheckAll' }));

    await waitFor(() => expect((action as HTMLButtonElement).disabled).toBe(false));
    expect(api.getMemorySettings).toHaveBeenCalledTimes(2);
  });

  it('refreshes Memory settings after dependency Repair changes recovery state', async () => {
    api.getMemorySettings
      .mockResolvedValueOnce(settings())
      .mockResolvedValue(settings(true));
    const user = userEvent.setup();
    renderPage();

    const repair = await screen.findByRole('button', { name: 'settings.dependencies.repair' });
    await waitFor(() => expect((repair as HTMLButtonElement).disabled).toBe(false));
    await user.click(repair);

    expect(await screen.findByRole('button', { name: 'memory.factoryReset.retry' })).toBeTruthy();
    expect(api.getMemorySettings).toHaveBeenCalledTimes(2);
  });

  it('derives Retry only from factory_reset_required and keeps it visible when Repair is required', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [memoryRuntime({ installed: false, status: 'missing', version: null })],
    });
    api.getMemorySettings.mockResolvedValue(settings(true));
    renderPage();

    const retry = await screen.findByRole('button', { name: 'memory.factoryReset.retry' });
    expect((retry as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('memory.factoryReset.artifactRepairRequired')).toBeTruthy();
  });

  it('does not derive Retry from a raw recovery_intent field', async () => {
    api.getMemorySettings.mockResolvedValue({ ...settings(), recovery_intent: 'factory_reset' });
    renderPage();

    expect(await screen.findByRole('button', { name: 'memory.factoryReset.button' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'memory.factoryReset.retry' })).toBeNull();
  });

  it('MEMORY-FACTORY-001 uses the destructive five-second confirmation with the exact scope', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'memory.factoryReset.button' }));

    const dialog = screen.getByTestId('reinitialize-confirm');
    expect(dialog.dataset.destructive).toBe('true');
    expect(dialog.dataset.holdSeconds).toBe('5');
    expect(dialog.dataset.title).toBe('memory.factoryReset.confirmTitle');
    expect(dialog.dataset.confirmLabel).toBe('memory.factoryReset.confirmLabel');
    expect(screen.getByText('memory.factoryReset.roots.primaryStorage.label')).toBeTruthy();
    expect(screen.getByText('memory.factoryReset.roots.primaryStorage.description')).toBeTruthy();
    expect(screen.getByText('memory.factoryReset.technicalPath:memory')).toBeTruthy();
    expect(screen.getByText('memory.factoryReset.roots.memoryStateStorage.label')).toBeTruthy();
    expect(screen.getByText('memory.factoryReset.roots.memoryStateStorage.description')).toBeTruthy();
    expect(screen.getByText('memory.factoryReset.technicalPath:state/memory')).toBeTruthy();
    expect(api.factoryResetMemory).not.toHaveBeenCalled();
  });

  it('disables Repair during reinitialization and refreshes both resources afterward', async () => {
    let finish: ((value: Awaited<ReturnType<typeof api.factoryResetMemory>>) => void) | undefined;
    api.factoryResetMemory.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const changed = vi.fn();
    window.addEventListener('avibe:memory-settings-changed', changed);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'memory.factoryReset.button' }));
    await user.click(screen.getByRole('button', { name: 'confirm-reinitialize' }));

    await waitFor(() => expect(api.factoryResetMemory).toHaveBeenCalledTimes(1));
    expect((screen.getByRole('button', { name: 'settings.dependencies.repair' }) as HTMLButtonElement).disabled).toBe(true);

    finish?.({
      ok: true,
      result: 'completed',
      data_deleted: true,
      data_remaining: false,
      roots: [
        { path: 'memory', existed: true, deleted: true },
        { path: 'state/memory', existed: true, deleted: true },
      ],
    });
    await waitFor(() => expect(api.listDependencies).toHaveBeenCalledTimes(2));
    expect(api.getMemorySettings).toHaveBeenCalledTimes(2);
    expect(changed).toHaveBeenCalledTimes(1);
    window.removeEventListener('avibe:memory-settings-changed', changed);
  });

  it('disables reinitialization while dependency Repair is pending', async () => {
    let finishInstall: ((value: { ok: boolean }) => void) | undefined;
    api.installDependency.mockReturnValue(new Promise((resolve) => { finishInstall = resolve; }));
    const user = userEvent.setup();
    renderPage();

    const repair = await screen.findByRole('button', { name: 'settings.dependencies.repair' });
    await waitFor(() => expect((repair as HTMLButtonElement).disabled).toBe(false));
    await user.click(repair);
    expect((screen.getByRole('button', { name: 'memory.factoryReset.button' }) as HTMLButtonElement).disabled).toBe(true);
    finishInstall?.({ ok: true });
  });

  it('disables Memory runtime Repair and explains why while the sidecar is available', async () => {
    api.getMemoryStatus.mockResolvedValue({
      status: 'ok',
      source: { status: 'available', observed_at: '2026-08-18T14:44:35.331Z', reason: null },
      health: { status: 'ok', version: '1.2.3', capabilities: {}, disabled_features: [], cascade: null, recorder: null },
    });
    renderPage();

    const repair = await screen.findByRole('button', { name: 'settings.dependencies.repair' });
    await waitFor(() => expect(screen.getByText('settings.dependencies.memoryRuntimeDisableBeforeRepair')).toBeTruthy());
    expect((repair as HTMLButtonElement).disabled).toBe(true);
    expect(api.installDependency).not.toHaveBeenCalled();
  });

  it('localizes a live-sidecar Repair refusal instead of showing the raw token', async () => {
    api.installDependency.mockResolvedValue({
      ok: false,
      message: 'memory_runtime_install_requires_disabled_memory',
      output: null,
      reason: 'memory_runtime_install_requires_disabled_memory',
    });
    const user = userEvent.setup();
    renderPage();

    const repair = await screen.findByRole('button', { name: 'settings.dependencies.repair' });
    await waitFor(() => expect((repair as HTMLButtonElement).disabled).toBe(false));
    await user.click(repair);
    expect(showToast).toHaveBeenCalledWith(
      'errors.memory_runtime_install_requires_disabled_memory',
      'error',
    );
  });

  it('MEMORY-FACTORY-002 closes confirmation and reports partial per-root outcomes', async () => {
    api.factoryResetMemory.mockResolvedValue({
      ok: false,
      result: 'partial',
      error: 'memory_factory_reset_failed',
      data_deleted: true,
      data_remaining: true,
      roots: [
        { path: 'memory', existed: true, deleted: true, error: 'ConfinedFilesystemError' },
        { path: 'state/memory', existed: false, deleted: false },
      ],
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'memory.factoryReset.button' }));
    await user.click(screen.getByRole('button', { name: 'confirm-reinitialize' }));

    await waitFor(() => expect(screen.queryByTestId('reinitialize-confirm')).toBeNull());
    expect(screen.getByText('memory.factoryReset.rootOutcome:memory.factoryReset.roots.primaryStorage.label:memory.factoryReset.partial')).toBeTruthy();
    expect(screen.getByText('memory.factoryReset.rootOutcome:memory.factoryReset.roots.memoryStateStorage.label:memory.factoryReset.absent')).toBeTruthy();
    expect(screen.getByText('memory.factoryReset.technicalPath:memory')).toBeTruthy();
    expect(screen.getByText('memory.factoryReset.technicalPath:state/memory')).toBeTruthy();
  });
});
