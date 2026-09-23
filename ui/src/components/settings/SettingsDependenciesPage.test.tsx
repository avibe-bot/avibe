/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsDependenciesPage } from './SettingsDependenciesPage';
import type { DependenciesResult, DependencyReadOptions } from '@/context/ApiContext';

const api = vi.hoisted(() => ({
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
  id: 'show-runtime',
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
