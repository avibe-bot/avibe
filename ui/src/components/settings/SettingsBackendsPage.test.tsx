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

const renderBackends = () => render(
  <I18nextProvider i18n={i18n}>
    <MemoryRouter initialEntries={['/settings/backends']}>
      <Routes>
        <Route path="/settings/backends" element={<SettingsBackendsPage />} />
        <Route path="/settings/backends/avibe" element={<SettingsBackendPage backend="avibe" />} />
      </Routes>
    </MemoryRouter>
  </I18nextProvider>,
);

const rowOf = async (name: string) => (await screen.findByText(name)).closest('.rounded-xl') as HTMLElement;

describe('built-in backend settings', () => {
  // The built-in Avibe Agent is part of the platform: listed first, always on,
  // and marked Built-in where every other backend has its enable switch.
  it('lists Avibe first as Built-in with no switch, and its page has no switch or native actions', async () => {
    renderBackends();

    const avibe = await rowOf('Avibe Agent');
    const rows = [...document.querySelectorAll('.rounded-xl')];
    expect(rows.map((row) => row.querySelector('span.font-semibold')?.textContent)).toEqual([
      'Avibe Agent', 'OpenCode', 'Claude Code', 'Codex',
    ]);
    expect(within(avibe).getByText('Built-in')).toBeTruthy();
    expect(within(avibe).queryByRole('switch')).toBeNull();
    expect(within(avibe).queryByTestId('lifecycle-avibe')).toBeNull();
    for (const name of ['OpenCode', 'Claude Code', 'Codex']) {
      const row = await rowOf(name);
      expect(within(row).getByRole('switch')).toBeTruthy();
      expect(within(row).queryByText('Built-in')).toBeNull();
    }
    await waitFor(() => expect(mocks.api.detectCli).toHaveBeenCalledTimes(3));
    expect(mocks.api.detectCli.mock.calls.map(([binary]) => binary).sort()).toEqual(['claude', 'codex', 'opencode']);
    const configure = within(avibe).getByRole('link', { name: 'Configure' });
    expect(configure.getAttribute('href')).toBe('/settings/backends/avibe');
    fireEvent.click(configure);

    await screen.findByRole('heading', { level: 1, name: 'Avibe Agent' });
    expect(await screen.findByText('Built-in')).toBeTruthy();
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: /Detect|Install|Restart|Save|Sign in/i })).toBeNull();
    expect(screen.queryByTestId('lifecycle-avibe')).toBeNull();
    expect(mocks.api.mutateConfig).not.toHaveBeenCalled();
    expect(mocks.api.detectCli).toHaveBeenCalledTimes(3);
    expect(mocks.api.getBackendRuntime).not.toHaveBeenCalled();
    expect(mocks.api.installAgent).not.toHaveBeenCalled();
    expect(mocks.api.restartBackend).not.toHaveBeenCalled();
  });
});

describe('backend attention', () => {
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
  const modelHubConfig = (enabled: boolean) => ({
    agents: {},
    capabilities: { model_hub: { enabled: true } },
    model_hub: { enabled, agents: { avibe: { mode: 'hub' }, claude: { mode: 'direct' } } },
  });

  it('flags the always-on backend whose Agent has no model', async () => {
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue(withoutModel);
    mocks.api.getConfig.mockResolvedValue(modelHubConfig(true));
    renderBackends();

    const avibe = await rowOf('Avibe Agent');
    expect(await within(avibe).findByText('No model selected')).toBeTruthy();
    expect(within(avibe).queryByText('Gateway off')).toBeNull();
    expect(within(await rowOf('Claude Code')).queryByText('No model selected')).toBeNull();
  });

  // Turning the gateway off leaves the backend enabled with nothing to run on;
  // its status names the cause and its page leads to Models.
  it('says the gateway is off instead, on the row and on the backend page', async () => {
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue(withoutModel);
    mocks.api.getConfig.mockResolvedValue(modelHubConfig(false));
    renderBackends();

    const avibe = await rowOf('Avibe Agent');
    expect(await within(avibe).findByText('Gateway off')).toBeTruthy();
    expect(within(avibe).queryByText('No model selected')).toBeNull();
    expect(within(await rowOf('Claude Code')).queryByText('Gateway off')).toBeNull();

    fireEvent.click(within(avibe).getByRole('link', { name: 'Configure' }));
    expect(await screen.findByText('The model gateway is off, so Avibe Agent has no model to run on.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open Models page' }).getAttribute('href')).toBe('/settings/models');
  });

  // The operator's Model Hub switch leaves the always-on backend nothing to run on,
  // and there is no Models page to open.
  it('says Model Hub is disabled when the instance turns it off', async () => {
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue(withoutModel);
    mocks.api.getConfig.mockResolvedValue({ agents: {}, capabilities: { model_hub: { enabled: false } } });
    renderBackends();

    const avibe = await rowOf('Avibe Agent');
    expect(await within(avibe).findByText('Model Hub disabled')).toBeTruthy();
    expect(within(await rowOf('Claude Code')).queryByText('Model Hub disabled')).toBeNull();

    fireEvent.click(within(avibe).getByRole('link', { name: 'Configure' }));
    expect(await screen.findByText('Model Hub is disabled on this instance, so Avibe Agent has no model to run on.')).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open Models page' })).toBeNull();
  });
});
