/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import type { Location } from 'react-router-dom';

import {
  SettingsOverlayOriginContext,
  settingsOverlayOpenState,
  useSettingsOverlayOrigin,
} from '@/lib/settingsOverlay';
import { SettingsLayout } from './SettingsLayout';

const api = vi.hoisted(() => {
  const configChangedHandlers = new Set<(config: unknown) => void>();
  return {
    configChangedHandlers,
    getConfig: vi.fn(),
    getVersion: vi.fn(),
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

const SettingsLayoutHarness = () => {
  const location = useLocation();
  const origin = useSettingsOverlayOrigin(location);
  return (
    <SettingsOverlayOriginContext.Provider value={origin}>
      <SettingsLayout />
      <NavigationProbe />
    </SettingsOverlayOriginContext.Provider>
  );
};

type SettingsTestEntry = string | {
  pathname: string;
  state?: unknown;
};

const renderLayout = (entry: SettingsTestEntry) => render(
  <MemoryRouter initialEntries={[entry]}>
    <Routes>
      <Route path="/settings" element={<SettingsLayoutHarness />}>
        <Route path="general" element={<div>general-body</div>} />
        <Route path="backends" element={<div>backends-body</div>} />
        <Route path="backends/claude" element={<div>claude-body</div>} />
        <Route path="models" element={<div>models-body</div>} />
        <Route path="replies" element={<div>replies-body</div>} />
        <Route path="shortcuts" element={<div>shortcuts-body</div>} />
        <Route path="service" element={<div>service-body</div>} />
        <Route path="platforms" element={<div>platforms-body</div>} />
        <Route path="platforms/users" element={<div>users-body</div>} />
        <Route path="platforms/groups" element={<div>groups-body</div>} />
        <Route path="remote-access" element={<div>remote-access-body</div>} />
      </Route>
      <Route path="/chat/:sessionId" element={<div>chat-body</div>} />
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
  api.getVersion.mockResolvedValue({
    current: '3.1.4',
    latest: '3.1.4',
    has_update: false,
    error: null,
    build: { kind: 'package' },
  });
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
  it('closes a desktop overlay back to its exact opening route', async () => {
    media.matches = true;
    const user = userEvent.setup();
    const origin: Location = {
      pathname: '/chat/ses_7',
      search: '?message=m1',
      hash: '#tail',
      state: { source: 'search' },
      key: 'chat-origin',
    };

    renderLayout({
      pathname: '/settings/replies',
      state: settingsOverlayOpenState(origin),
    });

    const close = screen.getByRole('button', { name: 'settings.close' });
    await user.click(close);
    expect(await screen.findByText('chat-body')).toBeTruthy();
  });

  it('keeps the settings rail fixed while the route pane owns vertical scrolling', () => {
    renderLayout('/settings/replies');

    const routePane = screen.getByText('replies-body').closest('section');
    const shell = routePane?.parentElement?.parentElement;
    const navigation = screen.getByRole('navigation', { name: 'settings.navigationLabel' });

    expect(routePane?.className).toContain('overflow-y-auto');
    expect(navigation.className).not.toContain('overflow-y-auto');
    // The return row is pinned above the scrolling section list, so the way out
    // of Settings never scrolls away from the top of the rail.
    const returnRow = navigation.firstElementChild;
    expect(returnRow?.getAttribute('aria-label')).toBe('settings.backToApp');
    expect(returnRow?.className).not.toContain('overflow-y-auto');
    expect(navigation.querySelector('.overflow-y-auto')).toBeTruthy();
    expect(shell?.className).toContain('h-full');
    expect(shell?.className).toContain('min-h-0');
    expect(shell?.className).toContain('overflow-hidden');
    expect(shell?.className).toContain('md:h-[var(--app-shell-h)]');
    expect(shell?.className).not.toContain('md:h-auto');
    expect(shell?.className).not.toContain('min-h-full');
  });

  it.each(['/settings/models', '/settings/models/'])(
    'lets Model Hub fill the route pane at %s',
    (path) => {
      renderLayout(path);

      const modelHubFrame = screen.getByText('models-body').parentElement;
      expect(modelHubFrame?.className).toContain('min-h-full');
      expect(modelHubFrame?.className).not.toContain('mx-auto');
      expect(modelHubFrame?.className).not.toContain('max-w-[1180px]');
    },
  );

  // The source draws General as content that simply fills whatever the rail
  // leaves, so the shared reading column would re-introduce a fixed width the
  // design does not have. It opts out the way Model Hub already does — without
  // Model Hub's `min-h-full`, which is a full-height pane, not a width.
  it.each(['/settings/general', '/settings/general/'])(
    'lets General fill the route pane at %s',
    (path) => {
      renderLayout(path);

      const generalFrame = screen.getByText('general-body').parentElement;
      expect(generalFrame?.className).not.toContain('mx-auto');
      expect(generalFrame?.className).not.toContain('max-w-[1180px]');
      expect(generalFrame?.className).not.toContain('min-h-full');
    },
  );

  it('keeps standard settings pages constrained', () => {
    renderLayout('/settings/replies');

    const standardFrame = screen.getByText('replies-body').parentElement;
    expect(standardFrame?.className).toContain('mx-auto');
    expect(standardFrame?.className).toContain('max-w-[1180px]');
    expect(standardFrame?.className).not.toContain('min-h-full');
  });

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
    expect(screen.getByRole('link', { name: 'nav.channels' }).className).toContain('bg-mint-soft');
    expect(screen.getByRole('link', { name: 'settings.sections.platformConnections' }).className).not.toContain('bg-mint-soft');
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

  it('moves language and appearance into General and keeps the account menu in the rail', () => {
    renderLayout('/settings/replies');

    // Language and appearance are a Settings SECTION now, reachable and
    // linkable like every other one — not two icons wedged into the rail
    // footer where nothing else about the interface lives.
    expect(screen.queryByTestId('language-switcher')).toBeNull();
    expect(screen.queryByTestId('theme-toggle')).toBeNull();
    expect(screen.getByRole('link', { name: 'settings.sections.general' }).getAttribute('href'))
      .toBe('/settings/general');
    expect(screen.getByTestId('account-menu').getAttribute('data-open-upward')).toBe('true');
  });

  it('keeps member preferences, Replies, and Access while preserving the phase-2 permission gate', () => {
    authorization.capabilities.can_manage_instance = false;
    renderLayout('/settings/replies');

    expect(screen.getByText('replies-body')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.replies' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.shortcuts' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.sections.access' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'settings.sections.service' })).toBeNull();
    expect(api.getVersion).not.toHaveBeenCalled();
    expect(api.getConfig).not.toHaveBeenCalled();
  });

  it('opens General from /settings even after a remembered Backends visit', async () => {
    // Ordinary Settings is a destination, not a resume: whatever the last visit
    // was, the landing is the section every role can read.
    window.localStorage.setItem('avibe.settings.last-path', '/settings/backends');
    media.matches = true;
    renderLayout('/settings');

    expect(await screen.findByText('general-body')).toBeTruthy();
    expect(screen.queryByText('backends-body')).toBeNull();
  });

  it.each([
    ['/settings/backends', 'backends-body'],
    ['/settings/platforms', 'platforms-body'],
    ['/settings/remote-access', 'remote-access-body'],
  ])('keeps the explicit deep link %s authoritative over the landing page', (path, body) => {
    media.matches = true;
    window.localStorage.setItem('avibe.settings.last-path', '/settings/service');
    renderLayout(path);

    expect(screen.getByText(body)).toBeTruthy();
    expect(screen.queryByText('general-body')).toBeNull();
  });

  it('keeps the narrow root as the explicit section list a detail page can return to', async () => {
    const user = userEvent.setup();
    media.matches = false;
    renderLayout('/settings');

    // This root is the phone's section-NAVIGATION screen, not its ordinary way
    // in: the dock chip goes straight to General at every width (see
    // MobileDockDrawer). Redirecting the root as well would leave General's back
    // row pointing at a screen that bounces the user back to General.
    const general = screen.getByRole('link', { name: 'settings.sections.general' });
    expect(general.getAttribute('href')).toBe('/settings/general');
    expect(screen.queryByText('general-body')).toBeNull();

    await user.click(general);
    expect(await screen.findByText('general-body')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'settings.backToSections' }).getAttribute('href')).toBe('/settings');
  });

  it('selects General when a mobile root viewport becomes desktop', async () => {
    renderLayout('/settings');

    expect(screen.queryByText('general-body')).toBeNull();
    act(() => {
      media.matches = true;
      media.listeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent));
    });

    await waitFor(() => expect(screen.getByText('general-body')).toBeTruthy());
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

  it('keeps mobile navigation and the Back action touch-sized', () => {
    renderLayout('/settings');

    expect(screen.getByRole('link', { name: 'settings.sections.replies' }).className).toContain('min-h-11');
    const back = screen.getByRole('link', { name: 'settings.backToWorkbench' });
    expect(back.getAttribute('href')).toBe('/');
    expect(back.className).toContain('size-11');
  });

  it('unwinds a retained overlay from the phone root instead of pushing a second home', async () => {
    // A phone that opened Settings over a surface worth keeping now has the same
    // origin the desktop controls close through, so its root back has to close
    // rather than navigate: pushing '/' would leave the whole Settings chain one
    // Back away from the user.
    const user = userEvent.setup();
    const origin: Location = {
      pathname: '/chat/ses_7',
      search: '?message=m1',
      hash: '#tail',
      state: { source: 'search' },
      key: 'chat-origin',
    };

    renderLayout({ pathname: '/settings', state: settingsOverlayOpenState(origin) });

    const back = screen.getByRole('button', { name: 'settings.backToWorkbench' });
    expect(back.className).toContain('size-11');
    expect(back.className).toContain('md:hidden');
    await user.click(back);
    expect(await screen.findByText('chat-body')).toBeTruthy();
  });

  it('keeps a retained origin on ordinary links inside settings', () => {
    // Only the root action leaves; a section's back still steps one level up the
    // rail, and does it as a link so the origin rides along with the push.
    renderLayout({
      pathname: '/settings/replies',
      state: settingsOverlayOpenState({
        pathname: '/chat/ses_7',
        search: '',
        hash: '',
        state: null,
        key: 'chat-origin',
      } as Location),
    });

    const back = screen.getByRole('link', { name: 'settings.backToSections' });
    expect(back.getAttribute('href')).toBe('/settings');
    expect(screen.queryByRole('button', { name: 'settings.backToSections' })).toBeNull();
  });

  it('shows the current version with a safe-area-aware mobile popup', async () => {
    const user = userEvent.setup();
    renderLayout('/settings');

    const version = await screen.findByTitle('v3.1.4');
    expect(version.parentElement?.parentElement?.className).toContain('md:hidden');

    await user.click(version);
    const popup = screen.getByText('dashboard.versionAndUpdate').closest('.fixed');
    expect(popup?.className).toContain('top-[calc(4.5rem+env(safe-area-inset-top))]');
    expect(popup?.className).toContain('max-h-[calc(100dvh-5.5rem-env(safe-area-inset-top))]');
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

  it.each([
    ['/settings/replies', '/settings', 'settings.backToSections'],
    ['/settings/platforms/groups', '/settings', 'settings.backToSections'],
    ['/settings/backends/claude', '/settings/backends', 'settings.backToSection'],
  ])('returns one mobile level from %s', (path, target, label) => {
    renderLayout(path);

    const back = screen.getByRole('link', { name: label });
    expect(back.getAttribute('href')).toBe(target);
    expect(back.className).toContain('md:hidden');
  });

  it('animates a mobile detail screen without carrying that motion onto desktop', () => {
    renderLayout('/settings/replies');

    const frame = screen.getByText('replies-body').parentElement;
    expect(frame?.className).toContain('slide-in-from-right-4');
    expect(frame?.className).toContain('md:animate-none');
  });
});
