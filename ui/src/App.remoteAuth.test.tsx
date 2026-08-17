/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { AuthGuard } from './App';
import { DENIED_INSTANCE_CAPABILITIES } from './lib/sessionInfo';
import { reportRemoteAuthorizationState, REMOTE_AUTH_STATE_EVENT } from './lib/remoteAuth';

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
}));

vi.mock('./context/ApiContext', () => ({
  useApi: () => api,
}));

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
