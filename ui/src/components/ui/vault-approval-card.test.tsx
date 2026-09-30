// @vitest-environment jsdom

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { VaultGrantDuration, VaultRequest } from '@/context/ApiContext';
import type { BlindBox } from '@/lib/vaultCrypto';
import en from '@/i18n/en.json';
import { VaultApprovalCard } from './vault-approval-card';

const api = vi.hoisted(() => ({
  createVaultAgentBindingsBatch: vi.fn(),
  fulfillVaultAccessRequest: vi.fn(),
  getVaultSettings: vi.fn(),
  saveVaultSettings: vi.fn(),
}));
const warmVaultSandboxClient = vi.hoisted(() => vi.fn());
const vault = vi.hoisted(() => ({
  status: 'locked',
  refresh: vi.fn(),
  approveProtectedRelease: vi.fn(),
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_use_vault_secrets: true } }),
}));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => vault }));
vi.mock('@/lib/vaultSandboxClient', async (loadOriginal) => ({
  ...(await loadOriginal<typeof import('@/lib/vaultSandboxClient')>()),
  warmVaultSandboxClient,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const envelope = { ciphertext: 'c2VhbGVk', nonce: 'bm9uY2U=', wrap_meta: '{}' };
const protectedAccess = {
  requester: {},
  status: 'pending',
  message_id: null,
  created_at: '2026-10-01T00:00:00Z',
  decided_at: null,
  expires_at: null,
  id: 'vrq_access',
  request_type: 'access',
  secret_name: 'PROTON_BRIDGE_SMTP_PASSWORD',
  delivery: {},
  card: {
    request_type: 'access',
    protection: 'protected',
    secret_names: ['PROTON_BRIDGE_SMTP_PASSWORD'],
    protected_secret_names: ['PROTON_BRIDGE_SMTP_PASSWORD'],
    grant_options: [
      { grant_id: 'grant_1', unlock_material: [{ name: 'PROTON_BRIDGE_SMTP_PASSWORD', kind: 'static', envelope }] },
    ],
  },
} satisfies VaultRequest;

// The batch endpoint's reply for one issue; each issue carries its own approval nonce.
const issuedFor = (grantDuration: VaultGrantDuration) => ({
  ok: true,
  request_id: 'vrq_access',
  grant_id: 'grant_1',
  grant_duration: grantDuration,
  agent_pubkey: { public_key: 'agent-pk', fingerprint: 'agent-fp' },
  items: [
    {
      name: 'PROTON_BRIDGE_SMTP_PASSWORD',
      context: { issuedFor: grantDuration },
      approval: { nonce: `nonce-${grantDuration}`, expires_at_unix: 1 },
    },
  ],
});
const blindBox = { scheme: 'hpke-x25519-hkdfsha256-aes256gcm-v1', enc: 'enc', ct: 'ct' } as unknown as BlindBox;

beforeEach(() => {
  // Only the card's refresh interval is faked, so promises and Testing Library keep real timers.
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  api.getVaultSettings.mockResolvedValue({ ok: true, settings: { last_grant_ttl: 300 } });
  api.saveVaultSettings.mockResolvedValue({ ok: true });
  api.fulfillVaultAccessRequest.mockResolvedValue({ ok: true });
  api.createVaultAgentBindingsBatch.mockImplementation(async ({ grant_duration }: { grant_duration: VaultGrantDuration }) =>
    issuedFor(grant_duration),
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

const renderCard = () =>
  render(
    <I18nextProvider i18n={i18n}>
      <VaultApprovalCard request={protectedAccess} onResolved={vi.fn()} onCancel={vi.fn()} />
    </I18nextProvider>,
  );

const openWindow = () => {
  const popup = { closed: false, close: vi.fn() };
  const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
  return { popup, open };
};

// Contract: the approval click of a protected access request opens the sandbox authorization
// window and hands the sandbox binding contexts issued while the card was open, for the duration
// being approved, without a daemon request after the window opens; the card starts the sandbox
// client and issues once, for the duration it shows, and stops issuing while the sandbox releases
// the contexts.
// A Home Screen app on iOS is frozen about two seconds after it opens a window, so a daemon round
// trip after the click (the regression) often leaves that window on its placeholder, and a
// refresh during the release supersedes the contexts it is releasing. The VaultsPage reveal test
// covers only the reveal context, not these binding contexts.
describe('VaultApprovalCard protected access approval', () => {
  it.each([
    { approving: 'the remembered duration', remembered: 900, pick: null, duration: 900, issuesBeforeClick: [900] },
    { approving: 'a duration picked on the card', remembered: 300, pick: /^15 min$/, duration: 900, issuesBeforeClick: [300, 900] },
  ])('claims the contexts issued before the click for $approving', async ({ remembered, pick, duration, issuesBeforeClick }) => {
    api.getVaultSettings.mockResolvedValue({ ok: true, settings: { last_grant_ttl: remembered } });
    let release: (boxes: BlindBox[]) => void = () => undefined;
    vault.approveProtectedRelease.mockImplementation(() => new Promise<BlindBox[]>((resolve) => (release = resolve)));
    const { popup, open } = openWindow();

    renderCard();
    await vi.waitFor(() => expect(api.createVaultAgentBindingsBatch).toHaveBeenCalled());
    if (pick) fireEvent.click(await screen.findByRole('radio', { name: pick }));
    await vi.waitFor(() =>
      expect(api.createVaultAgentBindingsBatch).toHaveBeenLastCalledWith({ request_id: 'vrq_access', grant_duration: duration }),
    );
    expect(api.createVaultAgentBindingsBatch.mock.calls.map(([payload]) => payload.grant_duration)).toEqual(issuesBeforeClick);

    fireEvent.click(screen.getByRole('button', { name: /^approve$/i }));

    expect(open).toHaveBeenCalledOnce();
    expect(warmVaultSandboxClient.mock.invocationCallOrder[0]).toBeLessThan(open.mock.invocationCallOrder[0]);
    await vi.waitFor(() => expect(vault.approveProtectedRelease).toHaveBeenCalledOnce());
    const [items, authorizationWindow] = vault.approveProtectedRelease.mock.calls[0];
    expect(authorizationWindow).toBe(new URL(String(open.mock.calls[0][0])).searchParams.get('id'));
    expect(items.map((item: { context: unknown }) => item.context)).toEqual([{ issuedFor: duration }]);

    vi.advanceTimersByTime(60_000);
    expect(api.createVaultAgentBindingsBatch).toHaveBeenCalledTimes(issuesBeforeClick.length);

    release([blindBox]);
    await vi.waitFor(() => expect(api.fulfillVaultAccessRequest).toHaveBeenCalledOnce());
    expect(api.fulfillVaultAccessRequest.mock.calls[0][1]).toMatchObject({
      grant_duration: duration,
      deks: [{ name: 'PROTON_BRIDGE_SMTP_PASSWORD', approval: { nonce: `nonce-${duration}` } }],
    });
    expect(popup.close).toHaveBeenCalledOnce();
  });

  // Contract: a claim judges the contexts' age from when their request left, not from when the
  // reply arrived. A page frozen while the request was out (iOS backgrounding) receives the reply
  // late; timing it from arrival (the regression) hands the sandbox contexts that are already too
  // old, or expired for a one-time grant. The case above never delays a reply, so it cannot see this.
  it('issues fresh contexts when the prepared request left too long ago', async () => {
    let answerFirst: () => void = () => undefined;
    const stale = { ...issuedFor(300), items: [{ ...issuedFor(300).items[0], context: { stale: true } }] };
    api.createVaultAgentBindingsBatch.mockImplementationOnce(() => new Promise((resolve) => (answerFirst = () => resolve(stale))));
    vault.approveProtectedRelease.mockResolvedValue([blindBox]);
    openWindow();
    const sent = Date.now();
    const now = vi.spyOn(Date, 'now');

    renderCard();
    await vi.waitFor(() => expect(api.createVaultAgentBindingsBatch).toHaveBeenCalledOnce());
    now.mockReturnValue(sent + 25_000);
    answerFirst();

    fireEvent.click(screen.getByRole('button', { name: /^approve$/i }));

    await vi.waitFor(() => expect(vault.approveProtectedRelease).toHaveBeenCalledOnce());
    expect(api.createVaultAgentBindingsBatch).toHaveBeenCalledTimes(2);
    expect(vault.approveProtectedRelease.mock.calls[0][0].map((item: { context: unknown }) => item.context)).toEqual([{ issuedFor: 300 }]);
  });
});
