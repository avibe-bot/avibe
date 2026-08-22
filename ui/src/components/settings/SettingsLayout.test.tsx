/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { SettingsLayout } from './SettingsLayout';

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getMemorySettings: vi.fn(),
}));
const authorization = vi.hoisted(() => ({
  capabilities: { can_manage_instance: true },
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const renderLayout = (path: string) => render(
  <MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/settings" element={<SettingsLayout />}>
        <Route path="appearance" element={<div>appearance-body</div>} />
        <Route path="replies" element={<div>replies-body</div>} />
      </Route>
    </Routes>
  </MemoryRouter>,
);

beforeEach(() => {
  authorization.capabilities.can_manage_instance = true;
  api.getConfig.mockResolvedValue({ capabilities: { model_hub: { enabled: true } } });
  api.getMemorySettings.mockResolvedValue({ status: 'ok', enabled: true });
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('SettingsLayout', () => {
  it('renders the four-group rail and feature-gated owner sections', async () => {
    renderLayout('/settings/appearance');

    expect(screen.getByText('appearance-body')).toBeTruthy();
    expect(screen.getByText('settings.groups.preferences')).toBeTruthy();
    expect(screen.getByText('settings.groups.agents')).toBeTruthy();
    expect(screen.getByText('settings.groups.connections')).toBeTruthy();
    expect(screen.getByText('settings.groups.system')).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'settings.sections.models' })).toBeTruthy();
      expect(screen.getByRole('link', { name: 'settings.sections.memory' })).toBeTruthy();
    });
  });

  it('keeps member preferences, Replies, and Access while preserving the phase-2 permission gate', () => {
    authorization.capabilities.can_manage_instance = false;
    renderLayout('/settings/replies');

    expect(screen.getByText('replies-body')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.appearance' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.account' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.replies' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.access' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'settings.sections.service' })).toBeNull();
    expect(api.getConfig).not.toHaveBeenCalled();
  });
});
