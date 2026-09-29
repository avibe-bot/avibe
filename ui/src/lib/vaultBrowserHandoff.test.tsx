// @vitest-environment jsdom

import { createInstance } from 'i18next';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { VaultRequest, VaultSecret } from '@/context/ApiContext';
import { VaultApprovalCard } from '@/components/ui/vault-approval-card';
import { VaultRequestCard } from '@/components/ui/vault-request-card';
import { VaultSecretForm } from '@/components/ui/vault-secret-form';
import { VaultsPage } from '@/components/workbench/VaultsPage';
import en from '@/i18n/en.json';

const api = vi.hoisted(() => ({
  connectWorkbenchEvents: vi.fn(),
  createVaultAgentBindingsBatch: vi.fn(),
  createVaultRevealContext: vi.fn(),
  denyVaultRequest: vi.fn(),
  fulfillVaultAccessRequest: vi.fn(),
  getVaultGrants: vi.fn(),
  getVaultRequests: vi.fn(),
  getVaultSettings: vi.fn(),
  listDependencies: vi.fn(),
  listSkills: vi.fn(),
  listVaultSecrets: vi.fn(),
  signVaultDigest: vi.fn(),
}));
const vault = vi.hoisted(() => ({
  status: 'needs-setup',
  error: null,
  setError: vi.fn(),
  refresh: vi.fn(),
  lock: vi.fn(),
  hasPasskey: vi.fn(),
  passkeyUsableHere: vi.fn(),
  setupPasskey: vi.fn(),
  unlockPasskey: vi.fn(),
  approveProtectedRelease: vi.fn(),
  signProtectedRequest: vi.fn(),
  revealProtectedValue: vi.fn(),
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_use_vault_secrets: true } }),
}));
vi.mock('@/lib/useProtectedVault', () => ({
  useProtectedVault: () => vault,
  useVaultLock: () => ({ unlocked: false, remainingMs: 0, lockNow: vi.fn() }),
  webauthnAvailable: () => true,
}));
// Workbench navigation, not part of the Vaults step (and it scrolls, which jsdom lacks).
vi.mock('@/components/workbench/CapabilityTabs', () => ({ CapabilityTabs: () => null }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const envelope = { ciphertext: 'c2VhbGVk', nonce: 'bm9uY2U=', wrap_meta: '{}' };
const pending = { requester: {}, status: 'pending', message_id: null, created_at: '2026-09-30T00:00:00Z', decided_at: null, expires_at: null };

const protectedAccess = {
  ...pending,
  id: 'vrq_access',
  request_type: 'access',
  secret_name: 'OPENAI_API_KEY',
  delivery: {},
  card: {
    request_type: 'access',
    protection: 'protected',
    secret_names: ['OPENAI_API_KEY'],
    protected_secret_names: ['OPENAI_API_KEY'],
    grant_options: [{ grant_id: 'grant_1', unlock_material: [{ name: 'OPENAI_API_KEY', kind: 'static', envelope }] }],
  },
} satisfies VaultRequest;

const protectedSign = {
  ...pending,
  id: 'vrq_sign',
  request_type: 'sign',
  secret_name: 'TREASURY_KEY',
  delivery: {
    digest: 'ab'.repeat(32),
    scheme: 'ecdsa-secp256k1-recoverable',
    signing_context: { chain_id: 1 },
    operation_context: { payload: 'e30', signature: 'c2ln' },
  },
  card: {
    request_type: 'sign',
    kind: 'keypair',
    protection: 'protected',
    secret_name: 'TREASURY_KEY',
    secret_unlock_material: { name: 'TREASURY_KEY', kind: 'keypair', envelope },
  },
} satisfies VaultRequest;

// The agent suggested Standard; the user answers on Protected. Shaped like the backend's
// `_secure_input_card`, which derives `default_protection` from the spec it also carries.
const standardSpec = { protection: 'standard' as const };
const standardProvision = {
  ...pending,
  id: 'vrq_provision',
  request_type: 'provision',
  secret_name: 'WALLET_PASSWORD',
  delivery: {},
  card: {
    card_type: 'secure_input',
    request_id: 'vrq_provision',
    secret_name: 'WALLET_PASSWORD',
    protection_options: ['standard', 'protected'],
    default_protection: 'standard',
    value: null,
    spec: standardSpec,
  },
} satisfies VaultRequest;

const protectedSecret: VaultSecret = {
  name: 'SEED_PHRASE',
  tags: [],
  kind: 'static',
  protection: 'protected',
  signer_kind: null,
  source: 'user',
  policy: {},
  last_used_at: null,
  use_count: 0,
  created_at: '2026-09-30T00:00:00Z',
  updated_at: '2026-09-30T00:00:00Z',
};

type Click = { role: 'button' | 'menuitem'; name: RegExp };
const approve: Click = { role: 'button', name: /^(approve|sign|continue in browser)$/i };
const protectedTier: Click = { role: 'button', name: /^protected/i };
const openInBrowser: Click = { role: 'button', name: /add a passkey|unlock with passkey|open in browser/i };

const resumedDialog = () => screen.findByRole('dialog');
const expectApprovalButton = async (name: RegExp) => {
  expect(within(await resumedDialog()).getByRole('button', { name })).toBeTruthy();
};
const expectProtectedForm = async (fixedName?: string) => {
  const dialog = within(await resumedDialog());
  expect(dialog.getByRole('button', { name: protectedTier.name }).getAttribute('aria-pressed')).toBe('true');
  if (fixedName) expect(dialog.getAllByText(fixedName).length).toBeGreaterThan(0);
};

const cases: Array<{
  surface: string;
  desktop: () => ReactElement;
  clicks: Click[];
  requests?: VaultRequest[];
  secrets?: VaultSecret[];
  vaultStatus?: string;
  resumed: () => Promise<void>;
}> = [
  {
    surface: 'protected access approval',
    desktop: () => <VaultApprovalCard request={protectedAccess} onResolved={vi.fn()} onCancel={vi.fn()} />,
    clicks: [approve],
    requests: [protectedAccess],
    resumed: () => expectApprovalButton(/^approve$/i),
  },
  {
    surface: 'protected sign approval',
    desktop: () => <VaultApprovalCard request={protectedSign} onResolved={vi.fn()} onCancel={vi.fn()} />,
    clicks: [approve],
    requests: [protectedSign],
    resumed: () => expectApprovalButton(/^sign$/i),
  },
  {
    surface: 'chat request card review',
    desktop: () => <VaultRequestCard request={protectedAccess} onResolved={vi.fn()} />,
    clicks: [{ role: 'button', name: /^review/i }],
    requests: [protectedAccess],
    resumed: () => expectApprovalButton(/^approve$/i),
  },
  {
    surface: 'Add switched to Protected',
    desktop: () => <VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} />,
    clicks: [protectedTier, openInBrowser],
    resumed: () => expectProtectedForm(),
  },
  {
    surface: 'provision request answered on Protected over its Standard default',
    desktop: () => (
      <VaultSecretForm
        fixedName="WALLET_PASSWORD"
        provisionRequestId="vrq_provision"
        requestSpec={standardSpec}
        defaultProtection="standard"
        onCancel={vi.fn()}
        onCreated={vi.fn()}
      />
    ),
    clicks: [protectedTier, openInBrowser],
    requests: [standardProvision],
    vaultStatus: 'locked',
    resumed: () => expectProtectedForm('WALLET_PASSWORD'),
  },
  {
    // An agent's `$WALLET_PASSWORD` ask whose request lookup found nothing: only the name identifies it.
    surface: 'secret an agent asked for by name, with no pending request',
    desktop: () => <VaultSecretForm fixedName="WALLET_PASSWORD" onCancel={vi.fn()} onCreated={vi.fn()} />,
    clicks: [protectedTier, openInBrowser],
    resumed: () => expectProtectedForm('WALLET_PASSWORD'),
  },
  {
    surface: 'protected secret reveal',
    desktop: () => <VaultsPage />,
    clicks: [
      { role: 'button', name: /^more actions$/i },
      { role: 'menuitem', name: /^show in browser$/i },
    ],
    secrets: [protectedSecret],
    resumed: async () => {
      expect(await screen.findByRole('menuitem', { name: /^show value$/i })).toBeTruthy();
    },
  },
];

const ceremonies = [vault.setupPasskey, vault.unlockPasskey, vault.approveProtectedRelease, vault.signProtectedRequest, vault.revealProtectedValue];

function renderAt(path: string, ui: ReactElement) {
  return render(
    <I18nextProvider i18n={i18n}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </I18nextProvider>,
  );
}

beforeEach(() => {
  vault.hasPasskey.mockReturnValue(true);
  vault.passkeyUsableHere.mockReturnValue(true);
  for (const call of Object.values(api)) call.mockResolvedValue({ ok: true, items: [], secrets: [], deps: [], skills: [] });
  api.connectWorkbenchEvents.mockReturnValue(() => undefined);
  api.getVaultGrants.mockResolvedValue({ grants: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
});

// Contract: in the desktop shell, whose WKWebView can't run WebAuthn and whose new windows never
// pair with the sandbox, every surface that would start a protected passkey step instead opens the
// same Workbench's Vaults page in the browser and starts nothing here; the browser's Vaults page
// resumes that same step (the request, a Protected Add for the same name, or the secret's reveal)
// and stops at the click that begins it. Each surface encodes its own step while Vaults decodes
// them all, so a step the page can't resume, or resumes with a lost part (a provision answer back
// on the request's Standard default, a reveal on a bare list, an agent's name dropped from Add),
// strands the user in the browser; only a round trip through both ends catches that.
describe('desktop shell protected-vault browser handoff', () => {
  it.each(cases)('$surface resumes in the browser without starting a passkey step', async (c) => {
    const user = userEvent.setup();
    vault.status = c.vaultStatus ?? 'needs-setup';
    api.getVaultRequests.mockResolvedValue({ requests: c.requests ?? [] });
    api.listVaultSecrets.mockResolvedValue({ secrets: c.secrets ?? [] });
    const open = vi.spyOn(window, 'open').mockReturnValue(null);

    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    renderAt('/vaults', c.desktop());
    for (const click of c.clicks) await user.click(await screen.findByRole(click.role, { name: click.name }));

    expect(open).toHaveBeenCalledTimes(1);
    const target = new URL(String(open.mock.calls[0][0]));
    expect(target.origin).toBe(window.location.origin);
    expect(target.pathname).toBe('/vaults');
    expect(screen.queryByRole('dialog')).toBeNull();

    cleanup();
    delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
    renderAt(`${target.pathname}${target.search}`, <VaultsPage />);
    await c.resumed();

    expect(open).toHaveBeenCalledTimes(1);
    for (const ceremony of ceremonies) expect(ceremony).not.toHaveBeenCalled();
    expect(api.createVaultAgentBindingsBatch).not.toHaveBeenCalled();
    expect(api.createVaultRevealContext).not.toHaveBeenCalled();
    expect(api.signVaultDigest).not.toHaveBeenCalled();
  });
});
