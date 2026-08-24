/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';

import { SettingsLayout } from './SettingsLayout';

const api = vi.hoisted(() => {
  const configChangedHandlers = new Set<(config: unknown) => void>();
  return {
    configChangedHandlers,
    getConfig: vi.fn(),
    getMemorySettings: vi.fn(),
    onConfigChanged: vi.fn((handler: (config: unknown) => void) => {
      configChangedHandlers.add(handler);
      return () => configChangedHandlers.delete(handler);
    }),
  };
});
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
vi.mock('../LanguageSwitcher', () => ({
  LanguageSwitcher: ({ openUpward }: { openUpward?: boolean }) => (
    <div data-testid="language-switcher" data-open-upward={String(openUpward)} />
  ),
}));
vi.mock('../ThemeToggle', () => ({ ThemeToggle: () => <div data-testid="theme-toggle" /> }));
vi.mock('../AccountMenu', () => ({
  AccountMenu: ({ openUpward }: { openUpward?: boolean }) => (
    <div data-testid="account-menu" data-open-upward={String(openUpward)} />
  ),
}));

const NavigationProbe = () => {
  const navigate = useNavigate();
  return (
    <div>
      <button type="button" onClick={() => navigate('/settings/service')}>go-service</button>
      <button type="button" onClick={() => navigate('/settings/platforms/groups')}>go-groups</button>
      <button type="button" onClick={() => navigate(-1)}>go-back</button>
    </div>
  );
};

const SettingsLayoutHarness = () => (
  <>
    <SettingsLayout />
    <NavigationProbe />
  </>
);

const renderLayout = (path: string) => render(
  <MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/settings" element={<SettingsLayoutHarness />}>
        <Route path="replies" element={<div>replies-body</div>} />
        <Route path="service" element={<div>service-body</div>} />
        <Route path="platforms" element={<div>platforms-body</div>} />
        <Route path="platforms/users" element={<div>users-body</div>} />
        <Route path="platforms/groups" element={<div>groups-body</div>} />
      </Route>
    </Routes>
  </MemoryRouter>,
);

beforeEach(() => {
  window.localStorage.clear();
  authorization.capabilities.can_manage_instance = true;
  media.matches = false;
  media.listeners.clear();
  api.configChangedHandlers.clear();
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

  it('opens Messaging Platforms by default and still allows manual collapse', async () => {
    const user = userEvent.setup();
    renderLayout('/settings/service');

    const platforms = screen.getByRole('button', { name: 'nav.messagingPlatforms' });
    expect(platforms.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('link', { name: 'settings.sections.platformConnections' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'nav.users' })).toBeTruthy();
    await waitFor(() => expect(screen.getByRole('link', { name: 'nav.channels' })).toBeTruthy());

    await user.click(platforms);
    expect(platforms.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('link', { name: 'settings.sections.platformConnections' })).toBeNull();

    await user.click(platforms);
    expect(platforms.getAttribute('aria-expanded')).toBe('true');
  });

  it('auto-expands Platforms and selects the matching nested destination', async () => {
    renderLayout('/settings/platforms/groups');

    expect(screen.getByText('groups-body')).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'nav.messagingPlatforms' }).getAttribute('aria-expanded')).toBe('true');
    });
    expect(screen.getByRole('link', { name: 'nav.channels' }).className).toContain('bg-mint/[0.09]');
    expect(screen.getByRole('link', { name: 'settings.sections.platformConnections' }).className).not.toContain('bg-mint/[0.09]');
    expect(screen.getByRole('link', { name: 'settings.sections.platformConnections' }).getAttribute('aria-current')).toBeNull();
  });

  it('hides Groups when no enabled platform supports group settings', async () => {
    api.getConfig.mockResolvedValue({
      capabilities: { model_hub: { enabled: true } },
      platforms: { enabled: ['wechat'] },
    });
    renderLayout('/settings/replies');

    expect(screen.getByRole('link', { name: 'settings.sections.platformConnections' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'nav.users' })).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('link', { name: 'nav.channels' })).toBeNull());
  });

  it('refreshes Groups visibility after successful platform config changes', async () => {
    api.getConfig.mockResolvedValue({
      capabilities: { model_hub: { enabled: true } },
      platforms: { enabled: ['wechat'] },
    });
    renderLayout('/settings/replies');

    await waitFor(() => expect(screen.queryByRole('link', { name: 'nav.channels' })).toBeNull());
    expect(screen.getByRole('link', { name: 'settings.sections.models' })).toBeTruthy();

    act(() => {
      api.configChangedHandlers.forEach((handler) => handler({
        capabilities: { model_hub: { enabled: true } },
        platforms: { enabled: ['slack'] },
      }));
    });
    expect(screen.getByRole('link', { name: 'nav.channels' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.models' })).toBeTruthy();

    act(() => {
      api.configChangedHandlers.forEach((handler) => handler({
        capabilities: { model_hub: { enabled: true } },
        platforms: { enabled: ['wechat'] },
      }));
    });
    expect(screen.queryByRole('link', { name: 'nav.channels' })).toBeNull();
    expect(screen.getByRole('link', { name: 'settings.sections.models' })).toBeTruthy();
  });

  it('restores the default expanded disclosure after navigation', async () => {
    const user = userEvent.setup();
    renderLayout('/settings/replies');

    expect(screen.getByRole('button', { name: 'nav.messagingPlatforms' }).getAttribute('aria-expanded')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'nav.messagingPlatforms' }));
    expect(screen.getByRole('button', { name: 'nav.messagingPlatforms' }).getAttribute('aria-expanded')).toBe('false');
    await user.click(screen.getByRole('button', { name: 'go-service' }));
    await user.click(screen.getByRole('button', { name: 'go-back' }));
    expect(screen.getByRole('button', { name: 'nav.messagingPlatforms' }).getAttribute('aria-expanded')).toBe('true');

    await user.click(screen.getByRole('button', { name: 'go-groups' }));
    expect(screen.getByRole('button', { name: 'nav.messagingPlatforms' }).getAttribute('aria-expanded')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'nav.messagingPlatforms' }));
    expect(screen.getByRole('button', { name: 'nav.messagingPlatforms' }).getAttribute('aria-expanded')).toBe('false');
    await user.click(screen.getByRole('button', { name: 'go-service' }));
    await user.click(screen.getByRole('button', { name: 'go-back' }));
    expect(screen.getByRole('button', { name: 'nav.messagingPlatforms' }).getAttribute('aria-expanded')).toBe('true');
  });

  it('keeps language, theme, and account preferences in the Settings rail', () => {
    renderLayout('/settings/replies');

    expect(screen.getByTestId('language-switcher').getAttribute('data-open-upward')).toBe('true');
    expect(screen.getByTestId('theme-toggle')).toBeTruthy();
    expect(screen.getByTestId('account-menu').getAttribute('data-open-upward')).toBe('true');
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
