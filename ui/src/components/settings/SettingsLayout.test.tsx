/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
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
const media = vi.hoisted(() => ({
  matches: false,
  listeners: new Set<(event: MediaQueryListEvent) => void>(),
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
        <Route path="replies" element={<div>replies-body</div>} />
      </Route>
    </Routes>
  </MemoryRouter>,
);

beforeEach(() => {
  window.localStorage.clear();
  authorization.capabilities.can_manage_instance = true;
  media.matches = false;
  media.listeners.clear();
  api.getConfig.mockResolvedValue({ capabilities: { model_hub: { enabled: true } } });
  api.getMemorySettings.mockResolvedValue({ status: 'ok', enabled: true });
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({
    get matches() {
      return media.matches;
    },
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => media.listeners.add(listener),
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => media.listeners.delete(listener),
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('SettingsLayout', () => {
  it('renders the three-group rail and feature-gated owner sections', async () => {
    renderLayout('/settings/replies');

    expect(screen.getByText('replies-body')).toBeTruthy();
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
    expect(screen.getByRole('link', { name: 'settings.sections.replies' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.access' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'settings.sections.service' })).toBeNull();
    expect(api.getConfig).not.toHaveBeenCalled();
  });

  it('selects the landing section when a mobile root viewport becomes desktop', async () => {
    renderLayout('/settings');

    expect(screen.queryByText('replies-body')).toBeNull();
    act(() => {
      media.matches = true;
      media.listeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent));
    });

    await waitFor(() => expect(screen.getByText('replies-body')).toBeTruthy());
  });

  it('refreshes Memory visibility after its settings change', async () => {
    renderLayout('/settings/replies');
    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'settings.sections.memory' })).toBeTruthy();
    });

    api.getMemorySettings.mockResolvedValueOnce({ status: 'ok', enabled: false });
    act(() => window.dispatchEvent(new Event('avibe:memory-settings-changed')));

    await waitFor(() => {
      expect(screen.queryByRole('link', { name: 'settings.sections.memory' })).toBeNull();
    });
  });

  it('keeps the mobile header below the safe-area inset', () => {
    renderLayout('/settings/replies');

    expect(screen.getByRole('banner').className).toContain('pt-[env(safe-area-inset-top)]');
  });

  it('keeps mobile navigation and detail controls above the bottom safe-area inset', () => {
    renderLayout('/settings/replies');

    expect(screen.getByRole('navigation', { name: 'settings.navigationLabel' }).className).toContain(
      'env(safe-area-inset-bottom)',
    );
    expect(screen.getByText('replies-body').closest('section')?.firstElementChild?.className).toContain(
      'env(safe-area-inset-bottom)',
    );
  });
});
