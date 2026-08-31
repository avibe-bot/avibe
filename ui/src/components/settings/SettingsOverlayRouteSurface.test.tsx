/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useEffect, useState } from 'react';
import {
  Link,
  MemoryRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from 'react-router-dom';

import {
  closeSettingsOverlay,
  useSettingsOverlayOrigin,
  useSettingsOverlayContext,
} from '@/lib/settingsOverlay';
import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { SettingsOverlayNavigationBoundary } from './SettingsOverlayNavigationBoundary';
import { SettingsOverlayRouteSurface } from './SettingsOverlayRouteSurface';

let chatMounts = 0;
let chatUnmounts = 0;

const ChatProbe = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const routeSurfaceActive = useRouteSurfaceActive();
  const [count, setCount] = useState(0);

  useEffect(() => {
    chatMounts += 1;
    return () => {
      chatUnmounts += 1;
    };
  }, []);

  useEffect(() => {
    if (!routeSurfaceActive) navigate('/escaped');
  }, [navigate, routeSurfaceActive]);

  useEffect(() => {
    const maintenance = (location.state as { maintenance?: string } | null)?.maintenance;
    if (!routeSurfaceActive && maintenance === 'pending') {
      navigate(`${location.pathname}${location.search}${location.hash}`, {
        replace: true,
        state: { maintenance: 'done' },
      });
    }
  }, [location, navigate, routeSurfaceActive]);

  return (
    <main>
      <div data-testid="chat-location">{`${location.pathname}${location.search}${location.hash}`}</div>
      <div data-testid="chat-count">{count}</div>
      <div data-testid="chat-active">{String(routeSurfaceActive)}</div>
      <div data-testid="chat-maintenance">
        {(location.state as { maintenance?: string } | null)?.maintenance ?? 'none'}
      </div>
      <button type="button" onClick={() => setCount((value) => value + 1)}>increment-chat</button>
      <Link to="/settings/replies">open-settings</Link>
      <Link to="/settings/diagnostics">open-diagnostics</Link>
      <Link to="/doctor">open-legacy-settings</Link>
    </main>
  );
};

const SettingsFrame = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayContext();
  const [count, setCount] = useState(0);

  return (
    <aside>
      <button
        type="button"
        onClick={() => {
          if (origin) closeSettingsOverlay(navigate, origin);
          else navigate('/');
        }}
      >
        close-settings
      </button>
      <button type="button" onClick={() => navigate('/settings/advanced')}>open-advanced</button>
      <button type="button" onClick={() => setCount((value) => value + 1)}>increment-settings</button>
      <div data-testid="settings-count">{count}</div>
      <div data-testid="settings-location">{location.pathname}</div>
      <Outlet />
    </aside>
  );
};

const settingsRoute = () => (
  <Route path="/settings" element={<SettingsFrame />}>
    <Route path="replies" element={<div>replies-settings</div>} />
    <Route path="advanced" element={<div>advanced-settings</div>} />
    <Route path="diagnostics" element={<div>diagnostics-settings</div>} />
  </Route>
);

const SettingsToggle = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayOrigin(location);
  return origin ? (
    <button
      type="button"
      data-settings-toggle="true"
      onClick={() => closeSettingsOverlay(navigate, origin)}
    >
      shell-settings
    </button>
  ) : (
    <Link to="/settings/replies" data-settings-toggle="true">shell-settings</Link>
  );
};

const Harness = ({ desktop }: { desktop: boolean }) => (
  <SettingsOverlayNavigationBoundary desktop={desktop}>
    <SettingsToggle />
    <SettingsOverlayRouteSurface fallbackElement={<Navigate to="/" replace />}>
      <Route path="/chat/:sessionId" element={<ChatProbe />} />
      <Route path="/escaped" element={<div>escaped-route</div>} />
      {settingsRoute()}
      <Route path="/doctor" element={<Navigate to="/settings/diagnostics" replace />} />
      <Route path="/" element={<div>workbench</div>} />
    </SettingsOverlayRouteSurface>
  </SettingsOverlayNavigationBoundary>
);

const RoutedHarness = ({ desktop = true }: { desktop?: boolean }) => (
  <Routes>
    <Route path="*" element={<Harness desktop={desktop} />} />
  </Routes>
);

const RemountingHarness = () => {
  const location = useLocation();
  const guardKey = location.pathname.startsWith('/settings/diagnostics') ? 'diagnostics' : 'default';
  return <Harness key={guardKey} desktop />;
};

beforeEach(() => {
  chatMounts = 0;
  chatUnmounts = 0;
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({
    matches: true,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('SettingsOverlayRouteSurface', () => {
  it('keeps the background route mounted across Settings navigation and close', async () => {
    const user = userEvent.setup();
    const view = render(
      <MemoryRouter initialEntries={[{
        pathname: '/chat/ses_1',
        search: '?view=chat',
        hash: '#tail',
        state: { maintenance: 'pending' },
      }]}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('button', { name: 'increment-chat' }));
    expect(screen.getByTestId('chat-count').textContent).toBe('1');
    expect(chatMounts).toBe(1);

    const settingsIngress = screen.getByRole('link', { name: 'shell-settings' });
    await user.click(settingsIngress);
    expect(screen.getByText('replies-settings')).toBeTruthy();
    const dialog = screen.getByRole('dialog', { name: 'nav.settings' });
    expect(dialog).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole('button', { name: 'close-settings' }),
    ));
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1?view=chat#tail');
    expect(screen.getByTestId('chat-count').textContent).toBe('1');
    expect(screen.getByTestId('chat-active').textContent).toBe('false');
    expect(screen.getByTestId('chat-maintenance').textContent).toBe('done');
    expect(screen.queryByText('escaped-route')).toBeNull();
    expect(chatMounts).toBe(1);
    expect(chatUnmounts).toBe(0);

    await user.click(screen.getByRole('button', { name: 'open-advanced' }));
    expect(screen.getByText('advanced-settings')).toBeTruthy();
    expect(screen.getByTestId('chat-count').textContent).toBe('1');
    expect(chatMounts).toBe(1);
    expect(chatUnmounts).toBe(0);

    await user.click(screen.getByRole('button', { name: 'increment-settings' }));
    expect(screen.getByTestId('settings-count').textContent).toBe('1');
    view.rerender(
      <MemoryRouter initialEntries={['/chat/ses_1?view=chat#tail']}>
        <RoutedHarness desktop={false} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('settings-count').textContent).toBe('1');

    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull();
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1?view=chat#tail');
    expect(screen.getByTestId('chat-count').textContent).toBe('1');
    expect(screen.getByTestId('chat-active').textContent).toBe('true');
    expect(screen.getByTestId('chat-maintenance').textContent).toBe('done');
    expect(chatMounts).toBe(1);
    expect(chatUnmounts).toBe(0);
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole('link', { name: 'shell-settings' }),
    ));
  });

  it('carries the origin through a guard remount', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <Routes>
          <Route path="*" element={<RemountingHarness />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'open-diagnostics' }));
    expect(await screen.findByText('diagnostics-settings')).toBeTruthy();
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(await screen.findByTestId('chat-location')).toBeTruthy();
  });

  it('closes when the persistent Settings toggle is clicked again', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'shell-settings' }));
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();
    const backdrop = document.querySelector<HTMLElement>('[data-dialog-surface-backdrop="true"]');
    expect(backdrop).not.toBeNull();
    await user.click(backdrop!);

    await waitFor(() => expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull());
    expect(screen.getByTestId('chat-location').textContent).toBe('/chat/ses_1');
  });

  it('keeps legacy redirects out of origins while preserving real ingress origins', async () => {
    const user = userEvent.setup();
    const view = render(
      <MemoryRouter initialEntries={['/chat/ses_1']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole('link', { name: 'open-legacy-settings' }));
    expect(await screen.findByText('diagnostics-settings')).toBeTruthy();
    expect(screen.getByRole('dialog', { name: 'nav.settings' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'close-settings' }));
    expect(await screen.findByTestId('chat-location')).toBeTruthy();

    view.unmount();
    render(
      <MemoryRouter initialEntries={['/doctor']}>
        <RoutedHarness />
      </MemoryRouter>,
    );
    expect(await screen.findByText('diagnostics-settings')).toBeTruthy();
    expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull();
  });

  it('renders a direct Settings URL as the primary route', () => {
    render(
      <MemoryRouter initialEntries={['/settings/replies']}>
        <RoutedHarness />
      </MemoryRouter>,
    );

    expect(screen.getByText('replies-settings')).toBeTruthy();
    expect(document.querySelector('[data-settings-overlay="true"]')).toBeNull();
    expect(screen.queryByTestId('chat-count')).toBeNull();
  });
});
