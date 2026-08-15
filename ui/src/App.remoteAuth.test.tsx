/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { AuthGuard } from './App';
import { DENIED_INSTANCE_CAPABILITIES } from './lib/sessionInfo';
import { REMOTE_AUTH_STATE_EVENT } from './lib/remoteAuth';

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
  cleanup();
  vi.clearAllMocks();
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

    render(
      <MemoryRouter initialEntries={['/']}>
        <AuthGuard><div>protected shell</div></AuthGuard>
      </MemoryRouter>,
    );

    expect(await screen.findByText('protected shell')).toBeTruthy();
    expect(api.getAuthSession).toHaveBeenCalledOnce();

    act(() => {
      window.dispatchEvent(new CustomEvent(REMOTE_AUTH_STATE_EVENT, {
        detail: { state: 'unavailable' },
      }));
      window.dispatchEvent(new CustomEvent(REMOTE_AUTH_STATE_EVENT, {
        detail: { state: 'current' },
      }));
    });

    await waitFor(() => expect(api.getAuthSession).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('protected shell')).toBeTruthy();
  });
});
