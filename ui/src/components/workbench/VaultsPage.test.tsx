/* @vitest-environment jsdom */

import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { VaultsPage } from './VaultsPage';

type WorkbenchEventHandlers = { onVaultsUpdated?: (data: unknown) => void };

const eventHandlers = vi.hoisted(() => [] as WorkbenchEventHandlers[]);
const api = vi.hoisted(() => ({
  connectWorkbenchEvents: vi.fn(() => () => undefined),
  getVaultAudit: vi.fn(),
  getVaultGrants: vi.fn(),
  getVaultRequests: vi.fn(),
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
  instanceRole: 'editor' as const,
  capabilities: {
    ...OWNER_INSTANCE_CAPABILITIES,
    can_manage_instance: false,
    can_use_vault_secrets: true,
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
  eventHandlers.length = 0;
  api.connectWorkbenchEvents.mockImplementation((handlers: WorkbenchEventHandlers) => {
    eventHandlers.push(handlers);
    return () => {
      const at = eventHandlers.indexOf(handlers);
      if (at >= 0) eventHandlers.splice(at, 1);
    };
  });
  api.getVaultRequests.mockResolvedValue({ requests: [] });
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
    expect(screen.getByRole('button', { name: 'vaults.add' })).toBeTruthy();

    await user.click(history);

    await waitFor(() => expect(api.getVaultAudit).toHaveBeenCalledWith({ limit: 50 }));
    expect(await screen.findByText('vaults.audit.title')).toBeTruthy();
    expect(await screen.findByText('grant_created')).toBeTruthy();
    expect(await screen.findByText('alpha')).toBeTruthy();
  });

  it('shows Vault management controls to an Editor', async () => {
    renderPage(activeOrgMember);

    expect(await screen.findByRole('button', { name: 'vaults.add' })).toBeTruthy();
    expect(screen.queryByText('vaults.remoteReadOnly')).toBeNull();
  });
});

// PERMISSIONS-016 — `vaults.updated` is admitted at the Editor tier that owns
// the endpoint it announces, and below runtime management the controller reduces
// the frame to its type alone. The pending-requests list is the consumer that
// has to wake on it, so it is driven here through the real page rather than
// asserted on the stream stub.
describe('PERMISSIONS-016 VaultsPage wakes an Editor on the bare vaults.updated frame', () => {
  it('refetches pending requests from the signal alone', async () => {
    renderPage(activeOrgMember);
    await waitFor(() => expect(api.getVaultRequests).toHaveBeenCalled());
    const before = api.getVaultRequests.mock.calls.length;

    await act(async () => {
      for (const handler of [...eventHandlers]) handler.onVaultsUpdated?.({});
    });

    await waitFor(() => expect(api.getVaultRequests.mock.calls.length).toBeGreaterThan(before));
    // The frame carried no secret name; the refetch still asks for this Editor's
    // own pending scope.
    const [query] = api.getVaultRequests.mock.calls.at(-1) ?? [];
    expect(query).toMatchObject({ status: 'pending' });
  });
});
