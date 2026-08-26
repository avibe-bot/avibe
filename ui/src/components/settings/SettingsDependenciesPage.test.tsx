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
  useTranslation: () => ({ t: (key: string) => key }),
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

describe('SettingsDependenciesPage Memory runtime', () => {
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
});
