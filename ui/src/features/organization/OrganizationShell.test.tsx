/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { InstanceAuthorizationContext, type InstanceAuthorizationValue } from '@/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { OrganizationShell } from './OrganizationShell';

const organizationState = vi.hoisted(() => ({
  gate: 'cloud_not_connected' as const,
  signIn: vi.fn(),
  retry: vi.fn(),
  selectOrganization: vi.fn(),
  signOut: vi.fn(),
  organizations: [],
  selectedOrganizationId: null,
  detail: {
    organization: { name: 'Acme' },
    membership: { role: 'owner' },
  },
  session: {
    user: { email: 'owner@example.com' },
    expires_in: 3600,
  },
}));

vi.mock('./context', () => ({
  useOrganization: () => organizationState,
}));

vi.mock('./OrganizationProvider', () => ({
  OrganizationProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function renderShell(capabilities: InstanceAuthorizationValue['capabilities']) {
  render(
    <InstanceAuthorizationContext.Provider
      value={{
        remote: true,
        instanceKind: 'organization',
        instanceRole: 'owner',
        capabilities,
      }}
    >
      <MemoryRouter>
        <OrganizationShell />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

describe('OrganizationShell cloud_not_connected action', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('keeps the remote access CTA reachable for owners', () => {
    renderShell(OWNER_INSTANCE_CAPABILITIES);

    const action = screen.getByRole('link', { name: 'organization.actions.openRemoteAccess' });
    expect(action.getAttribute('href')).toBe('/admin/remote-access');
  });

  it('sends remote owners to a reachable control-panel destination', () => {
    renderShell({
      ...OWNER_INSTANCE_CAPABILITIES,
      can_use_system: false,
    });

    const actions = screen.getAllByRole('link', { name: 'organization.actions.backToControlPanel' });
    expect(actions.every((action) => action.getAttribute('href') === '/admin/settings/messaging')).toBe(true);
  });
});
