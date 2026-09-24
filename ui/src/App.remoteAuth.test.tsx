/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect, useLayoutEffect } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';

import { AuthGuard } from './App';
import { Summary } from './components/steps/Summary';
import { useInstanceAuthorization } from './context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES, OWNER_INSTANCE_CAPABILITIES } from './lib/sessionInfo';
import { isOwnerOnlyPath } from './lib/adminNavigation';
import { reportRemoteAuthorizationState, REMOTE_AUTH_STATE_EVENT } from './lib/remoteAuth';
import { DESKTOP_OPEN_SETTINGS_EVENT } from './lib/desktopShell';
import { DesktopSettingsCommand } from './components/DesktopSettingsCommand';
import { SettingsOverlayNavigationBoundary } from './components/settings/SettingsOverlayNavigationBoundary';

vi.hoisted(() => {
  vi.stubGlobal('matchMedia', vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
});

const api = vi.hoisted(() => ({
  getAuthSession: vi.fn(),
  getConfig: vi.fn(),
  mutateConfig: vi.fn(),
}));
const status = vi.hoisted(() => ({ control: vi.fn() }));

vi.mock('./context/ApiContext', () => ({
  useApi: () => api,
}));
vi.mock('./context/StatusContext', () => ({ useStatus: () => status }));
vi.mock('./context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(() => {
  reportRemoteAuthorizationState('current');
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('AuthGuard remote authorization recovery', () => {
  it('refreshes the session when an in-shell outage recovers in the same tick', async () => {
    const session = {
      remote: true as const,
      authenticated: true as const,
      email: 'member@example.com',
      instance_kind: 'organization' as const,
      instance_role: 'viewer' as const,
      capabilities: {
        ...DENIED_INSTANCE_CAPABILITIES,
        can_read_instance: true,
      },
      authorization_state: 'current' as const,
    };
    api.getAuthSession.mockResolvedValue(session);
    let recoveryReported = false;

    const SameTickRecoverySignal = () => {
      useLayoutEffect(() => {
        if (recoveryReported) return;
        recoveryReported = true;
        window.dispatchEvent(new CustomEvent(REMOTE_AUTH_STATE_EVENT, {
          detail: { state: 'unavailable' },
        }));
        window.dispatchEvent(new CustomEvent(REMOTE_AUTH_STATE_EVENT, {
          detail: { state: 'current' },
        }));
      }, []);
      return <div>protected shell</div>;
    };

    render(
      <MemoryRouter initialEntries={['/']}>
        <AuthGuard><SameTickRecoverySignal /></AuthGuard>
      </MemoryRouter>,
    );

    await waitFor(() => expect(api.getAuthSession).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('protected shell')).toBeTruthy();
  });

  it('automatically probes again after an unavailable cold load', async () => {
    vi.useFakeTimers();
    api.getAuthSession
      .mockResolvedValueOnce({
        remote: true,
        authenticated: true,
        email: 'member@example.com',
        instance_kind: 'organization',
        authorization_state: 'unavailable',
      })
      .mockResolvedValue({
        remote: true,
        authenticated: true,
        email: 'member@example.com',
        instance_kind: 'organization',
        instance_role: 'viewer',
        capabilities: {
          ...DENIED_INSTANCE_CAPABILITIES,
          can_read_instance: true,
        },
        authorization_state: 'current',
      });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      json: async () => ({
        remote: true,
        authenticated: true,
        authorization_state: 'current',
      }),
    }));

    render(
      <MemoryRouter initialEntries={['/']}>
        <AuthGuard><div>protected shell</div></AuthGuard>
      </MemoryRouter>,
    );
    await act(async () => undefined);
    expect(screen.getByText('remoteAuthorization.unavailable.body')).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    await act(async () => undefined);

    expect(api.getAuthSession).toHaveBeenCalledTimes(2);
    expect(screen.getByText('protected shell')).toBeTruthy();
  });
});

const CapabilityProbe = () => {
  const { capabilities } = useInstanceAuthorization();
  return <div>{capabilities.can_manage_instance ? 'owner-shell' : 'denied-shell'}</div>;
};

const OwnerOnlyGate = () => {
  const { capabilities } = useInstanceAuthorization();
  if (isOwnerOnlyPath('/settings/diagnostics') && !capabilities.can_manage_instance) {
    return <Navigate to="/" replace />;
  }
  return <div>diagnostics-page</div>;
};

describe('AuthGuard setup-bypass authorization', () => {
  const localOwnerSession = {
    remote: false as const,
    instance_kind: 'personal' as const,
    instance_role: 'owner' as const,
    capabilities: OWNER_INSTANCE_CAPABILITIES,
  };

  it.each(['/settings/diagnostics', '/settings/diagnostics/logs'])(
    'keeps the instance owner on %s instead of treating the setup bypass as denied',
    async (path) => {
      api.getAuthSession.mockResolvedValue(localOwnerSession);
      api.getConfig.mockResolvedValue({
        mode: 'v2',
        setup_state: { needs_setup: false },
      });

      render(
        <MemoryRouter initialEntries={[path]}>
          <AuthGuard>
            <CapabilityProbe />
          </AuthGuard>
        </MemoryRouter>,
      );

      expect(await screen.findByText('owner-shell')).toBeTruthy();
      expect(screen.queryByText('denied-shell')).toBeNull();
    },
  );

  it('lets an owner open Diagnostics without bouncing home', async () => {
    api.getAuthSession.mockResolvedValue(localOwnerSession);
    api.getConfig.mockResolvedValue({
      mode: 'v2',
      setup_state: { needs_setup: false },
    });

    render(
      <MemoryRouter initialEntries={['/settings/diagnostics']}>
        <AuthGuard>
          <Routes>
            <Route path="/settings/diagnostics" element={<OwnerOnlyGate />} />
            <Route path="/" element={<div>workbench-home</div>} />
          </Routes>
        </AuthGuard>
      </MemoryRouter>,
    );

    expect(await screen.findByText('diagnostics-page')).toBeTruthy();
    expect(screen.queryByText('workbench-home')).toBeNull();
  });

  it('lets an authenticated owner open Model Hub before setup without completing setup', async () => {
    api.getAuthSession.mockResolvedValue(localOwnerSession);
    api.getConfig.mockResolvedValue({
      mode: 'v2',
      setup_state: { needs_setup: true },
      capabilities: { model_hub: { enabled: true } },
    });

    render(
      <MemoryRouter initialEntries={['/settings/models']}>
        <AuthGuard>
          <div>model-hub-page</div>
        </AuthGuard>
      </MemoryRouter>,
    );

    expect(await screen.findByText('model-hub-page')).toBeTruthy();
    expect(api.getConfig).toHaveBeenCalledOnce();
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });
});

describe('AuthGuard General Settings over setup', () => {
  it('opens the desktop Settings request over the wizard without leaving it', async () => {
    api.getAuthSession.mockResolvedValue({
      remote: false,
      instance_kind: 'personal',
      instance_role: 'owner',
      capabilities: OWNER_INSTANCE_CAPABILITIES,
    });
    api.getConfig.mockResolvedValue({ mode: 'v2', setup_state: { needs_setup: true } });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    const mounts = { count: 0 };
    const SetupSurface = () => {
      const { pathname } = useLocation();
      useEffect(() => { mounts.count += 1; }, []);
      return <div>at {pathname}</div>;
    };

    try {
      render(
        <MemoryRouter initialEntries={['/setup']}>
          <AuthGuard>
            <SettingsOverlayNavigationBoundary desktop>
              <DesktopSettingsCommand />
              <SetupSurface />
            </SettingsOverlayNavigationBoundary>
          </AuthGuard>
        </MemoryRouter>,
      );
      expect(await screen.findByText('at /setup')).toBeTruthy();

      act(() => {
        window.dispatchEvent(new Event(DESKTOP_OPEN_SETTINGS_EVENT, { cancelable: true }));
      });

      expect(await screen.findByText('at /settings/general')).toBeTruthy();
      expect(mounts.count).toBe(1);
      expect(api.getConfig).toHaveBeenCalledOnce();
    } finally {
      Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
    }
  });
});

describe('AuthGuard setup access parity (AUTH-SETUP-405)', () => {
  const managers = [
    { remote: false, instance_kind: 'personal', instance_role: 'owner' },
    { remote: true, instance_kind: 'personal', instance_role: 'owner' },
    { remote: true, instance_kind: 'organization', instance_role: 'owner' },
    { remote: true, instance_kind: 'organization', instance_role: 'member' },
  ] as const;

  const connect = (context: typeof managers[number], needsSetup: boolean) => {
    api.getAuthSession.mockResolvedValue({
      ...context,
      authenticated: true,
      email: 'owner@example.com',
      capabilities: {
        ...OWNER_INSTANCE_CAPABILITIES,
        is_instance_owner: context.instance_role === 'owner',
        can_manage_access_members: context.instance_role === 'owner',
      },
      authorization_state: 'current',
    });
    api.getConfig.mockResolvedValue({
      mode: 'self_host',
      setup_state: { needs_setup: needsSetup },
    });
  };

  const renderSetupRoutes = (path: string) => render(
    <MemoryRouter initialEntries={[path]}>
      <AuthGuard>
        <Routes>
          <Route path="/" element={<div>workbench-home</div>} />
          <Route path="/setup" element={
            <Summary
              data={{ platforms: { enabled: [] } }}
              onNext={() => undefined}
              onBack={() => undefined}
              isFirst={false}
              isLast
            />
          } />
        </Routes>
      </AuthGuard>
    </MemoryRouter>,
  );

  it.each(managers)(
    'completes setup and returns home for $instance_kind $instance_role (remote: $remote)',
    async (context) => {
      connect(context, true);
      api.mutateConfig.mockImplementation(async () => {
        api.getConfig.mockResolvedValue({
          mode: 'self_host',
          setup_state: { needs_setup: false },
        });
        return { setup_completed: true, platforms: { enabled: [] } };
      });
      status.control.mockResolvedValue(undefined);
      const user = userEvent.setup();
      renderSetupRoutes('/');

      await user.click(await screen.findByRole('button', { name: 'summary.finishAndStart' }));

      expect(api.mutateConfig).toHaveBeenCalledWith(expect.arrayContaining([
        { kind: 'set', path: ['setup_completed'], value: true },
      ]));
      expect(status.control).toHaveBeenCalledWith('start');
      expect(await screen.findByText('workbench-home', {}, { timeout: 2_000 })).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'summary.finishAndStart' })).toBeNull();
    },
  );

  it.each(managers)('keeps completed $instance_kind $instance_role at home (remote: $remote)', async (context) => {
    connect(context, false);
    renderSetupRoutes('/');
    expect(await screen.findByText('workbench-home')).toBeTruthy();
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });

  it.each(managers)('allows explicit setup for configured $instance_kind $instance_role (remote: $remote)', async (context) => {
    connect(context, false);
    renderSetupRoutes('/setup');
    expect(await screen.findByRole('button', { name: 'summary.finishAndStart' })).toBeTruthy();
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });

  it.each(['editor', 'viewer'])('does not force a remote %s to complete instance setup', async (role) => {
    api.getAuthSession.mockResolvedValue({
      remote: true,
      authenticated: true,
      instance_kind: 'organization',
      instance_role: role,
      authorization_state: 'current',
      capabilities: { ...DENIED_INSTANCE_CAPABILITIES, can_read_instance: true },
    });
    renderSetupRoutes('/');
    expect(await screen.findByText('workbench-home')).toBeTruthy();
    expect(api.getConfig).not.toHaveBeenCalled();
  });

  it('keeps a revoked remote session out of setup', async () => {
    api.getAuthSession.mockResolvedValue({
      remote: true,
      authenticated: true,
      instance_kind: 'organization',
      authorization_state: 'revoked',
    });
    renderSetupRoutes('/setup');
    expect(await screen.findByText('remoteAuthorization.revoked.body')).toBeTruthy();
    expect(api.getConfig).not.toHaveBeenCalled();
    expect(api.mutateConfig).not.toHaveBeenCalled();
  });
});
