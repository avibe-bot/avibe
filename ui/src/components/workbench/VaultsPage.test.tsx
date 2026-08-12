/* @vitest-environment jsdom */

import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { VaultsPage } from './VaultsPage';

const api = vi.hoisted(() => ({
  connectWorkbenchEvents: vi.fn(() => () => undefined),
  getVaultAudit: vi.fn(),
  getVaultGrants: vi.fn(),
  listVaultSecrets: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());
const revealProtectedValue = vi.hoisted(() => vi.fn());
const remoteOwner = {
  remote: true,
  instanceRole: 'owner' as const,
  capabilities: {
    ...OWNER_INSTANCE_CAPABILITIES,
    can_use_system: false,
  },
};

const activeOrgMember = {
  remote: true,
  instanceRole: 'viewer' as const,
  hasTemporaryUnrestrictedOrgAccess: true,
  capabilities: {
    ...OWNER_INSTANCE_CAPABILITIES,
    can_manage_instance: false,
    can_use_vault_secrets: false,
    can_use_system: false,
  },
};

vi.mock('../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('../../context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('../../lib/useProtectedVault', () => ({
  useProtectedVault: () => ({ revealProtectedValue }),
  useVaultLock: () => ({ unlocked: false, remainingMs: 0, lockNow: vi.fn() }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('./CapabilityTabs', () => ({
  CapabilityTabs: () => <div data-testid="capability-tabs" />,
}));

vi.mock('./WorkbenchPageHeader', () => ({
  WorkbenchPageHeader: ({ title, actions }: { title: ReactNode; actions?: ReactNode }) => (
    <div>
      <h1>{title}</h1>
      <div>{actions}</div>
    </div>
  ),
}));

function renderPage(context = remoteOwner) {
  return render(
    <InstanceAuthorizationContext.Provider value={context}>
      <MemoryRouter initialEntries={['/vaults']}>
        <VaultsPage />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

beforeEach(() => {
  api.listVaultSecrets.mockResolvedValue({ secrets: [] });
  api.getVaultGrants.mockResolvedValue({ grants: [] });
  api.getVaultAudit.mockResolvedValue({
    events: [{ id: 'audit-1', event: 'grant_created', secret_name: 'alpha', ts: '2026-08-11T00:00:00Z' }],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('VaultsPage remote audit history', () => {
  it('keeps the History button reachable for a remote owner and loads audit rows', async () => {
    const user = userEvent.setup();

    renderPage();

    const history = await screen.findByRole('button', { name: 'vaults.history' });
    expect(screen.queryByRole('button', { name: 'vaults.add' })).toBeNull();

    await user.click(history);

    await waitFor(() => expect(api.getVaultAudit).toHaveBeenCalledWith({ limit: 50 }));
    expect(await screen.findByText('vaults.audit.title')).toBeTruthy();
    expect(await screen.findByText('grant_created')).toBeTruthy();
    expect(await screen.findByText('alpha')).toBeTruthy();
  });

  it('shows Vault management controls to an active Organization member', async () => {
    renderPage(activeOrgMember);

    expect(await screen.findByRole('button', { name: 'vaults.add' })).toBeTruthy();
    expect(screen.queryByText('vaults.remoteReadOnly')).toBeNull();
  });
});
