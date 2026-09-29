// @vitest-environment jsdom

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { VaultRequest } from '@/context/ApiContext';
import { VaultApprovalCard } from '@/components/ui/vault-approval-card';
import { VaultRequestCard } from '@/components/ui/vault-request-card';
import { VaultSecretForm } from '@/components/ui/vault-secret-form';
import en from '@/i18n/en.json';

const api = vi.hoisted(() => ({
  getVaultSettings: vi.fn(),
  listDependencies: vi.fn(),
  listSkills: vi.fn(),
  listVaultSecrets: vi.fn(),
  createVaultAgentBindingsBatch: vi.fn(),
  fulfillVaultAccessRequest: vi.fn(),
  signVaultDigest: vi.fn(),
  denyVaultRequest: vi.fn(),
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
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_use_vault_secrets: true } }),
}));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => vault, webauthnAvailable: () => true }));

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

const onCancel = vi.fn();
const approvalAction = { name: /^(approve|sign|continue in browser)$/i };
const openInBrowser = { name: /add a passkey|unlock with passkey|open in browser/i };
const cases: Array<{ surface: string; ui: () => ReactElement; action: { name: RegExp }; search: string; setup?: () => void }> = [
  {
    surface: 'protected access approval',
    ui: () => <VaultApprovalCard request={protectedAccess} onResolved={vi.fn()} onCancel={onCancel} />,
    action: approvalAction,
    search: `?request_id=${protectedAccess.id}`,
  },
  {
    surface: 'protected sign approval',
    ui: () => <VaultApprovalCard request={protectedSign} onResolved={vi.fn()} onCancel={onCancel} />,
    action: approvalAction,
    search: `?request_id=${protectedSign.id}`,
  },
  {
    surface: 'chat request card review',
    ui: () => <VaultRequestCard request={protectedAccess} onResolved={vi.fn()} />,
    action: { name: /^review/i },
    search: `?request_id=${protectedAccess.id}`,
  },
  {
    // No request to reopen, so the browser starts a new Add on the protected tier.
    surface: 'protected vault setup while adding a secret',
    ui: () => <VaultSecretForm defaultProtection="protected" onCancel={onCancel} onCreated={vi.fn()} />,
    action: openInBrowser,
    search: '?add=protected',
  },
  {
    surface: 'protected vault unlock while answering a provision request',
    ui: () => (
      <VaultSecretForm
        fixedName="WALLET_PASSWORD"
        provisionRequestId="vrq_provision"
        defaultProtection="protected"
        onCancel={onCancel}
        onCreated={vi.fn()}
      />
    ),
    action: openInBrowser,
    search: '?request_id=vrq_provision',
    setup: () => {
      vault.status = 'locked';
    },
  },
];

beforeEach(() => {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
  vault.status = 'needs-setup';
  vault.hasPasskey.mockReturnValue(true);
  vault.passkeyUsableHere.mockReturnValue(true);
  vault.approveProtectedRelease.mockResolvedValue([]);
  vault.signProtectedRequest.mockResolvedValue({ signature: 'c2ln' });
  for (const call of Object.values(api)) call.mockResolvedValue({ ok: true, items: [], secrets: [], deps: [], skills: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
});

// Contract: in the desktop shell, whose WKWebView can't run WebAuthn and whose new windows never
// pair with the sandbox, every surface that would start a protected passkey step instead opens the
// same Workbench's Vaults page in the browser, reopening the same flow there (the request, or a new
// Add on the protected tier), and starts nothing here. Before this, each of these clicks began a
// ceremony (and, for access, issued binding contexts) that could only fail, leaving dead browser
// tabs behind; a bare Vaults page would strand an Add in progress with nothing to continue.
describe('desktop shell protected-vault browser handoff', () => {
  it.each(cases)('$surface reopens its flow in the browser without starting a passkey step', async ({ ui, action, search, setup }) => {
    setup?.();
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    render(<I18nextProvider i18n={i18n}>{ui()}</I18nextProvider>);

    fireEvent.click(await screen.findByRole('button', action));
    await Promise.resolve();

    expect(open).toHaveBeenCalledTimes(1);
    const target = new URL(String(open.mock.calls[0][0]));
    expect(target.origin).toBe(window.location.origin);
    expect(target.pathname).toBe('/vaults');
    expect(target.search).toBe(search);
    expect(screen.queryByRole('dialog')).toBeNull();
    for (const ceremony of [vault.setupPasskey, vault.unlockPasskey, vault.approveProtectedRelease, vault.signProtectedRequest]) {
      expect(ceremony).not.toHaveBeenCalled();
    }
    expect(api.createVaultAgentBindingsBatch).not.toHaveBeenCalled();
    expect(api.signVaultDigest).not.toHaveBeenCalled();
  });
});
