/* @vitest-environment jsdom */

import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { VaultsPage } from './VaultsPage';

type WorkbenchEventHandlers = { onVaultsUpdated?: (data: unknown) => void };

const eventHandlers = vi.hoisted(() => [] as WorkbenchEventHandlers[]);
const api = vi.hoisted(() => ({
  connectWorkbenchEvents: vi.fn(() => () => undefined),
  createVaultRevealContext: vi.fn(),
  getVaultAudit: vi.fn(),
  getVaultGrants: vi.fn(),
  getVaultRequests: vi.fn(),
  listVaultSecrets: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());
const revealProtectedValue = vi.hoisted(() => vi.fn());
const useVaultSandboxWarm = vi.hoisted(() => vi.fn((_enabled: boolean) => true));
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
  useVaultSandboxWarm,
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
  vi.restoreAllMocks();
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

// A popup opened after an await has lost the click's user activation and is blocked, which would
// silently bring back the sandbox's own launcher card and its second approval click. A Home Screen
// app on iOS is also frozen about two seconds after it opens a window, so nothing that window needs
// may be fetched after the click: the open row menu keeps a fresh reveal context, Show and Copy wait
// for it and for the sandbox client, and one secret's context never waits on another's. A menu
// fetch that failed is not kept, or the click would replay that failure, and a menu held open past
// the context's freshness bound refreshes it rather than fetching after the click.
describe('VaultsPage protected reveal', () => {
  const protectedSecret = (name: string) => ({
    name,
    tags: [],
    kind: 'static',
    protection: 'protected',
    signer_kind: null,
    source: 'user',
    policy: {},
    last_used_at: null,
    use_count: 0,
    created_at: '2026-08-11T00:00:00Z',
    updated_at: '2026-08-11T00:00:00Z',
  });
  const envelope = { ciphertext: 'c', nonce: 'n', wrap_meta: 'w' };

  beforeEach(() => {
    // Only the menu's refresh interval is faked, so promises and Testing Library keep real timers.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    api.listVaultSecrets.mockResolvedValue({ secrets: [protectedSecret('alpha')] });
    revealProtectedValue.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const showValue = () => screen.getByRole('menuitem', { name: 'vaults.reveal.show' }) as HTMLButtonElement;

  const openMenu = async (index = 0) => {
    const user = userEvent.setup();
    if (index === 0) renderPage();
    const [button] = (await screen.findAllByRole('button', { name: 'vaults.rowActions' })).slice(index);
    await user.click(button);
  };

  const clickShowValue = async () => {
    const popup = { closed: false, close: vi.fn() };
    const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    await vi.waitFor(() => expect(showValue().disabled).toBe(false));
    fireEvent.click(showValue());
    // Asserted synchronously after the click dispatch, before any awaited work can run.
    expect(open).toHaveBeenCalledOnce();
    const opened = new URL(String(open.mock.calls[0][0]));
    expect(opened.searchParams.get('mode')).toBe('authorize');
    await vi.waitFor(() => expect(revealProtectedValue).toHaveBeenCalledOnce());
    expect(revealProtectedValue.mock.calls[0][2]).toBe(opened.searchParams.get('id'));
    await vi.waitFor(() => expect(popup.close).toHaveBeenCalledOnce());
    return { open };
  };

  it.each([
    { menuFetch: 'succeeded', menuReply: { ok: true, context: { secret_name: 'alpha', menu: true } }, fetches: 1, revealed: { secret_name: 'alpha', menu: true } },
    { menuFetch: 'failed', menuReply: { ok: false, code: 'vault_error', message: 'transient' }, fetches: 2, revealed: { secret_name: 'alpha' } },
  ])('opens the sandbox authorization window inside the click and hands it a good context when the menu fetch $menuFetch', async ({ menuReply, fetches, revealed }) => {
    api.createVaultRevealContext
      .mockResolvedValueOnce({ ...menuReply, envelope })
      .mockResolvedValue({ ok: true, context: { secret_name: 'alpha' }, envelope });

    await openMenu();
    await clickShowValue();

    expect(revealProtectedValue.mock.calls[0][1]).toEqual(revealed);
    expect(api.createVaultRevealContext).toHaveBeenCalledTimes(fetches);
  });

  it('keeps Show value waiting while the context or the sandbox client are not ready', async () => {
    let answer: () => void = () => undefined;
    api.createVaultRevealContext.mockImplementation(
      () => new Promise((resolve) => (answer = () => resolve({ ok: true, context: { secret_name: 'alpha' }, envelope }))),
    );
    useVaultSandboxWarm.mockImplementation((enabled: boolean) => !enabled);

    await openMenu();
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledOnce());
    expect(showValue().disabled).toBe(true);
    answer();
    await vi.waitFor(() => expect(useVaultSandboxWarm).toHaveBeenLastCalledWith(true));
    expect(showValue().disabled).toBe(true);

    useVaultSandboxWarm.mockImplementation(() => true);
    await clickShowValue();
    expect(api.createVaultRevealContext).toHaveBeenCalledOnce();
  });

  it('prepares a second secret without waiting on the first one', async () => {
    api.listVaultSecrets.mockResolvedValue({ secrets: [protectedSecret('alpha'), protectedSecret('beta')] });
    api.createVaultRevealContext.mockImplementation((name: string) =>
      name === 'alpha' ? new Promise(() => undefined) : Promise.resolve({ ok: true, context: { secret_name: name }, envelope }),
    );

    await openMenu();
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledWith('alpha', expect.anything()));
    fireEvent.keyDown(document, { key: 'Escape' });
    await openMenu(1);
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledWith('beta', expect.anything()));
    await clickShowValue();

    expect(revealProtectedValue.mock.calls[0][1]).toEqual({ secret_name: 'beta' });
  });

  it('refreshes the context while the menu stays open, so the click fetches nothing', async () => {
    let fetched = 0;
    api.createVaultRevealContext.mockImplementation(async () => ({ ok: true, context: { fetch: ++fetched }, envelope }));
    const opened = Date.now();
    const now = vi.spyOn(Date, 'now');

    await openMenu();
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledOnce());
    now.mockReturnValue(opened + 20_000);
    vi.advanceTimersByTime(20_000);
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledTimes(2));
    now.mockReturnValue(opened + 35_000);
    const { open } = await clickShowValue();

    expect(revealProtectedValue.mock.calls[0][1]).toEqual({ fetch: 2 });
    expect(api.createVaultRevealContext).toHaveBeenCalledTimes(2);
    for (const order of api.createVaultRevealContext.mock.invocationCallOrder) {
      expect(order).toBeLessThan(open.mock.invocationCallOrder[0]);
    }
  });
});
