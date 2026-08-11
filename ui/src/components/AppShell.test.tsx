/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { AppShell } from './AppShell';

vi.hoisted(() => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
});

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getMemorySettings: vi.fn(),
}));
const status = vi.hoisted(() => ({ state: 'ready' as const }));
const inbox = vi.hoisted(() => ({ totalUnread: 0 }));
const instanceAuth = vi.hoisted(() => ({
  remote: true,
  capabilities: {
    can_manage_instance: true,
    can_use_system: false,
    can_manage_agents: false,
    can_manage_projects: false,
    can_use_skills: false,
    can_use_vault_secrets: false,
    can_use_files: false,
    can_use_terminal: false,
  },
}));

vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../context/StatusContext', () => ({ useStatus: () => ({ status }) }));
vi.mock('../context/WorkbenchInboxContext', () => ({ useWorkbenchInbox: () => inbox }));
vi.mock('../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => instanceAuth }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

beforeEach(() => {
  api.getConfig.mockResolvedValue({ platforms: { enabled: [] } });
  api.getMemorySettings.mockResolvedValue({
    status: 'failed',
    error: 'memory_settings_remote_only',
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('AppShell setup recovery', () => {
  it('shows the remote-owner recovery card instead of the wizard', async () => {
    render(
      <MemoryRouter initialEntries={['/setup']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/setup" element={<div data-testid="wizard">wizard</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText('setup.remoteOwner.title')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'setup.remoteOwner.action' }).getAttribute('href')).toBe(
      '/admin/settings/messaging',
    );
    expect(screen.queryByTestId('wizard')).toBeNull();
  });
});
