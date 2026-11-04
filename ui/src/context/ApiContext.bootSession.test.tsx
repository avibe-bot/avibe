/* @vitest-environment jsdom */

import { useEffect } from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { ApiProvider, useApi } from './ApiContext';
import { readBootSession, recordBootSession } from '../lib/bootSession';
import { OWNER_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';

// A cold start boots from the last confirmed session (AuthGuard). Signing out
// must leave the next start waiting for the server, even when a guard check
// that began before the sign-out answers "ready" while the logout request is
// still out, and even when that request fails.

const reader = {
  remote: true as const,
  authenticated: true as const,
  email: 'alice@example.com',
  instance_kind: 'personal' as const,
  instance_role: 'owner' as const,
  capabilities: OWNER_INSTANCE_CAPABILITIES,
  authorization_state: 'current' as const,
  author_id: 'remote:alice',
};

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('signing out', () => {
  it('forgets the boot session and refuses a late guard check that would record it again', async () => {
    let api: ReturnType<typeof useApi> | null = null;
    const Capture = () => {
      const value = useApi();
      useEffect(() => { api = value; }, [value]);
      return null;
    };
    let failLogout: (error: Error) => void = () => {};
    apiFetch.mockImplementation(async (path: string) => (
      path === '/auth/logout'
        ? new Promise<Response>((_resolve, reject) => { failLogout = reject; })
        : Response.json({})
    ));
    render(<ApiProvider><Capture /></ApiProvider>);
    await waitFor(() => expect(api).not.toBeNull());
    recordBootSession(reader);

    const signingOut = api!.signOut().catch(() => undefined);
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/auth/logout', expect.anything()));
    expect(readBootSession()).toBeNull();

    // The guard check that started before the sign-out answers now.
    recordBootSession(reader);
    expect(readBootSession()).toBeNull();

    failLogout(new Error('offline'));
    await signingOut;
    expect(readBootSession()).toBeNull();
  });
});
