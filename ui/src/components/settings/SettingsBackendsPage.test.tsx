/* @vitest-environment jsdom */
import type { ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { modelsApi } from './models/modelsApi';
import type { AgentSupply } from './models/types';
import { SettingsBackendPage } from './SettingsBackendPage';
import { SettingsBackendsPage } from './SettingsBackendsPage';

const mocks = vi.hoisted(() => ({
  api: {
    getConfig: vi.fn(),
    detectCli: vi.fn(),
    mutateConfig: vi.fn(),
    getBackendRuntime: vi.fn(),
    installAgent: vi.fn(),
    restartBackend: vi.fn(),
  },
  showToast: vi.fn(),
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => mocks.api }));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: mocks.showToast }) }));
vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({ children, title }: { children: ReactNode; title: string }) => <main><h1>{title}</h1>{children}</main>,
}));
vi.mock('./models/useModelHubCapability', () => ({ useModelHubCapability: () => false }));
vi.mock('./BackendLifecycleChip', () => ({
  BackendLifecycleChip: ({ name }: { name: string }) => <span data-testid={`lifecycle-${name}`} />,
}));

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage('en');
  mocks.api.getConfig.mockResolvedValue({ agents: {} });
  mocks.api.detectCli.mockImplementation(async (binary: string) => ({ found: true, path: binary }));
  mocks.api.mutateConfig.mockResolvedValue({ agents: { avibe: { enabled: true } } });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('in-process backend settings', () => {
  // Native-only fixtures never reached the missing-CLI default or showed which
  // lifecycle the Avibe row inherited when added to the backend catalog.
  it('opens Avibe settings from the backend list and saves availability without native actions', async () => {
    render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={['/settings/backends']}>
          <Routes>
            <Route path="/settings/backends" element={<SettingsBackendsPage />} />
            <Route path="/settings/backends/avibe" element={<SettingsBackendPage backend="avibe" />} />
          </Routes>
        </MemoryRouter>
      </I18nextProvider>,
    );

    await screen.findByText('Avibe Agent');
    for (const name of ['Claude Code', 'Codex', 'OpenCode', 'Avibe Agent']) {
      expect(screen.getByText(name)).toBeTruthy();
    }
    const label = screen.getByText('Avibe Agent');
    const row = label.closest('.rounded-xl') as HTMLElement;
    expect(within(row).getByRole('switch').getAttribute('aria-checked')).toBe('false');
    expect(within(row).queryByTestId('lifecycle-avibe')).toBeNull();
    await waitFor(() => expect(mocks.api.detectCli).toHaveBeenCalledTimes(3));
    expect(mocks.api.detectCli.mock.calls.map(([binary]) => binary).sort()).toEqual(['claude', 'codex', 'opencode']);
    const configure = within(row).getByRole('link', { name: 'Configure' });
    expect(configure.getAttribute('href')).toBe('/settings/backends/avibe');
    fireEvent.click(configure);

    await screen.findByRole('heading', { level: 1, name: 'Avibe Agent' });
    await screen.findByRole('switch');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: /Detect|Install|Restart|Save|Sign in/i })).toBeNull();
    expect(screen.queryByTestId('lifecycle-avibe')).toBeNull();
    const toggle = screen.getByRole('switch');
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    fireEvent.click(toggle);
    await waitFor(() => expect(mocks.api.mutateConfig).toHaveBeenCalledWith([
      { kind: 'set', path: ['agents', 'avibe', 'enabled'], value: true },
    ]));
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    expect(mocks.api.detectCli).toHaveBeenCalledTimes(3);
    expect(mocks.api.getBackendRuntime).not.toHaveBeenCalled();
    expect(mocks.api.installAgent).not.toHaveBeenCalled();
    expect(mocks.api.restartBackend).not.toHaveBeenCalled();
  });
});

describe('backend attention', () => {
  it('flags the backend whose Agent has no model once enabling it creates that Agent', async () => {
    const withoutModel = [
      {
        backend: 'avibe', cli_present: false, mode: 'hub', menu_kind: 'fixed',
        named_agents: [{ name: 'avibe', effective_model_id: null, supply_status: null }],
      },
      {
        backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed',
        named_agents: [{ name: 'claude', effective_model_id: 'claude-opus-5-5', supply_status: 'ok' }],
      },
    ] as AgentSupply[];
    // Disabled, the backend has no named Agent yet; enabling it creates the built-in one.
    const listAgents = vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce(withoutModel.slice(1))
      .mockResolvedValue(withoutModel);
    render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={['/settings/backends']}>
          <SettingsBackendsPage />
        </MemoryRouter>
      </I18nextProvider>,
    );
    const row = async (name: string) => (await screen.findByText(name)).closest('.rounded-xl') as HTMLElement;
    const avibe = await row('Avibe Agent');
    await waitFor(() => expect(listAgents).toHaveBeenCalledTimes(1));
    expect(within(avibe).queryByText('No model selected')).toBeNull();

    fireEvent.click(within(avibe).getByRole('switch'));

    expect(await within(avibe).findByText('No model selected')).toBeTruthy();
    expect(within(await row('Claude Code')).queryByText('No model selected')).toBeNull();
  });
});
