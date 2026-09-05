/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsDependenciesPage } from './SettingsDependenciesPage';
import type { DependenciesResult, DependencyReadOptions, MemoryStatusResult } from '@/context/ApiContext';

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

const stubDependencies = (response: DependenciesResult) => {
  api.listDependencies.mockImplementation(async ({ ids }: DependencyReadOptions = {}) => ({
    ...response,
    deps: (ids ?? response.deps.map((dep) => dep.id)).map((id) => (
      response.deps.find((dep) => dep.id === id)
      ?? { id, kind: 'runtime', required: null, installed: null, version: null, status: 'unknown', action_class: 'none' }
    )),
  }));
};

const renderPage = () => render(
  <MemoryRouter>
    <SettingsDependenciesPage />
  </MemoryRouter>,
);

beforeEach(() => {
  stubDependencies({ ok: true, deps: [dependency()] });
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
    stubDependencies({
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
    stubDependencies({
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
    stubDependencies({
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

describe('SettingsDependenciesPage independent checks', () => {
  it('shows completed checks and permits their actions while another check waits', async () => {
    stubDependencies({ ok: true, deps: [dependency({ id: 'avault' })] });
    const read = api.listDependencies.getMockImplementation()!;
    let finish!: (value: DependenciesResult) => void;
    const slow = new Promise<DependenciesResult>((resolve) => { finish = resolve; });
    api.listDependencies.mockImplementation((options: DependencyReadOptions) => (
      options.ids?.includes('askill') ? slow : read(options)
    ));
    renderPage();

    expect(await screen.findByText('settings.dependencies.statusReady · v1.0.0')).toBeTruthy();
    expect(screen.getByText('settings.dependencies.checking')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'settings.dependencies.reinstall' }));
    await waitFor(() => expect(api.installDependency).toHaveBeenCalledWith('avault'));
    expect(api.listDependencies).toHaveBeenLastCalledWith({ ids: ['avault'], signal: expect.any(AbortSignal) });
    await act(async () => finish({ ok: true, deps: [dependency({ id: 'askill', status: 'unknown' })] }));
    expect(screen.queryByText('settings.dependencies.checking')).toBeNull();
  });

  it('shows an unknown CLI version as a warning, never ready', async () => {
    stubDependencies({ ok: true, deps: [dependency({ id: 'askill', status: 'unknown', version: null })] });
    renderPage();
    const badges = await screen.findAllByText('settings.dependencies.statusUnknown');
    expect(badges.every((badge) => badge.className.includes('text-gold-ink'))).toBe(true);
    expect(screen.queryByText(/settings.dependencies.statusReady/)).toBeNull();
  });

  it('keeps stored failure evidence, blocks actions and retries only the failed check', async () => {
    const dep = dependency({ id: 'model-hub-engine', status: 'error', reason: 'engine_install_failed' });
    stubDependencies({ ok: true, deps: [dep] });
    const read = api.listDependencies.getMockImplementation()!;
    renderPage();
    expect(await screen.findByText('errors.engine_install_failed')).toBeTruthy();
    api.listDependencies.mockImplementation((options: DependencyReadOptions) => (
      options.ids?.includes(dep.id) ? Promise.reject(new Error('offline')) : read(options)
    ));
    await userEvent.click(screen.getByRole('button', { name: 'settings.dependencies.recheckAll' }));
    const retry = await screen.findByRole('button', { name: 'settings.dependencies.recheck' });
    expect(screen.getByText('errors.engine_install_failed')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'settings.dependencies.repair' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.installDependency).not.toHaveBeenCalled();
    api.listDependencies.mockImplementation(read);
    api.listDependencies.mockClear();
    await userEvent.click(retry);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'settings.dependencies.recheck' })).toBeNull());
    expect(api.listDependencies).toHaveBeenCalledTimes(1);
    expect(api.listDependencies).toHaveBeenCalledWith({ ids: [dep.id], signal: expect.any(AbortSignal) });
  });

  it('does not present an old healthy inspection as current after a failed refresh', async () => {
    stubDependencies({ ok: true, deps: [dependency({ id: 'avault' })] });
    const read = api.listDependencies.getMockImplementation()!;
    renderPage();
    expect(await screen.findByText('settings.dependencies.statusReady · v1.0.0')).toBeTruthy();
    api.listDependencies.mockImplementation((options: DependencyReadOptions) => (
      options.ids?.includes('avault') ? Promise.reject(new Error('offline')) : read(options)
    ));
    await userEvent.click(screen.getByRole('button', { name: 'settings.dependencies.recheckAll' }));
    await screen.findByRole('button', { name: 'settings.dependencies.recheck' });
    expect(screen.queryByText(/settings.dependencies.statusReady/)).toBeNull();
    expect((screen.getByRole('button', { name: 'settings.dependencies.reinstall' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('SettingsDependenciesPage Memory runtime', () => {
  it('does not let an older sidecar read override the current repair guard', async () => {
    let finish!: (value: MemoryStatusResult) => void;
    api.getMemoryStatus.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    renderPage();
    const repair = await screen.findByRole('button', { name: 'settings.dependencies.repair' });
    expect((repair as HTMLButtonElement).disabled).toBe(true);
    const running = {
      status: 'ok', state: 'running', reason: null,
      source: { status: 'unavailable', observed_at: null, reason: null }, health: null,
    } as MemoryStatusResult;
    api.getMemoryStatus.mockResolvedValue(running);
    await userEvent.click(screen.getByRole('button', { name: 'settings.dependencies.recheckAll' }));
    await screen.findByText('settings.dependencies.memoryRuntimeDisableBeforeRepair');
    await act(async () => finish({ ...running, state: 'disabled' }));
    expect((repair as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('settings.dependencies.memoryRuntimeDisableBeforeRepair')).toBeTruthy();
  });

  it.each([false, null, true])(
    'presents source management neutrally without claiming installation or readiness (%s)',
    async (installed) => {
      const row = Object.freeze(dependency({
        id: 'memory-package',
        required: true,
        installed,
        status: 'error',
        readiness: 'not_ready',
        action_class: 'operator_only',
        version: null,
        reason: 'memory_package_source_build',
      }));
      stubDependencies({ ok: true, deps: [row] });
      api.getMemoryStatus.mockRejectedValue(new Error('Status is unavailable'));
      renderPage();

      const badge = await screen.findByText('settings.dependencies.statusSourceManaged');
      expect(badge.className).not.toContain('destructive');
      expect(badge.className).toContain('text-muted');
      expect(screen.getByText('settings.dependencies.memoryPackageSourceManaged').className).toContain('text-muted');
      expect(screen.queryByRole('alert')).toBeNull();
      expect(screen.queryByText('settings.dependencies.statusReady')).toBeNull();
      expect(screen.queryByText('settings.dependencies.statusMissing')).toBeNull();
      expect(screen.queryByText('settings.dependencies.statusError')).toBeNull();
      expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual([
        'settings.dependencies.recheckAll',
      ]);
      expect(api.installDependency).not.toHaveBeenCalled();
      expect(row).toMatchObject({ installed, status: 'error', readiness: 'not_ready' });
    },
  );

  it.each([
    'memory_package_runtime_unavailable',
    'memory_package_artifact_unavailable',
    'memory_runtime_install_failed',
  ])('keeps the actual runtime failure visible next to a source-managed package: %s', async (reason) => {
    stubDependencies({
      ok: true,
      deps: [
        dependency({
          id: 'memory-package',
          installed: false,
          status: 'error',
          action_class: 'operator_only',
          reason: 'memory_package_source_build',
        }),
        dependency({ installed: false, status: 'error', action_class: 'none', reason }),
      ],
    });
    renderPage();

    expect(await screen.findByText('settings.dependencies.statusSourceManaged')).toBeTruthy();
    expect(screen.getByText('settings.dependencies.statusError').className).toContain('destructive');
    expect(screen.getByRole('alert').textContent).toBe(`errors.${reason}`);
    expect(api.installDependency).not.toHaveBeenCalled();
  });

  it.each([
    'memory_package_unpublished_build',
    'memory_package_metadata_unreadable',
    'memory_package_metadata_ambiguous',
    'memory_package_install_failed',
  ])('does not reclassify a different package failure: %s', async (reason) => {
    stubDependencies({
      ok: true,
      deps: [dependency({
        id: 'memory-package',
        installed: false,
        status: 'error',
        action_class: 'operator_only',
        reason,
      })],
    });
    renderPage();

    expect((await screen.findByText('settings.dependencies.statusError')).className).toContain('destructive');
    expect(screen.getByRole('alert').textContent).toBe(`errors.${reason}`);
    expect(screen.queryByText('settings.dependencies.statusSourceManaged')).toBeNull();
    expect(api.installDependency).not.toHaveBeenCalled();
  });

  it('routes repairable Python package bootstrap through its own dependency action', async () => {
    stubDependencies({
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
    stubDependencies({
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
    stubDependencies({
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
    stubDependencies({
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
