/* @vitest-environment jsdom */

// Protected seed-phrase wallets in the Add key dialog (contract rev0.7; ui/design:
// vault/mnemonic/protected/*). The page never handles words or a passphrase: the click opens the
// sandbox window, the opaque result is saved, and the wallet is offered only after the daemon's
// snapshot matches that result exactly. Once saving started, no failure is called "not saved".
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactNode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';

import en from '@/i18n/en.json';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import { VaultSandboxError } from '@/lib/vaultSandboxClient';
import { isHdWalletBlocked, unblockHdWallet } from '@/lib/vaultHdQuarantine';

import { VaultSecretForm } from './vault-secret-form';

const wallet = JSON.parse(readFileSync(resolve(__dirname, '../../lib/__fixtures__/protected-hd-public-wallet.json'), 'utf8')) as {
  envelope: { ciphertext: string; nonce: string; wrap_meta: string };
  envelope_sha256: string;
  reordered_envelope_sha256: string;
  accounts: Array<Record<string, unknown>>;
  expected_account0: Record<string, string>;
};

const api = vi.hoisted(() => ({
  listDependencies: vi.fn(async () => ({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.12' }] })),
  listVaultSecrets: vi.fn(async () => ({ secrets: [] })),
  listSkills: vi.fn(async () => ({ skills: [] })),
  createVaultSecret: vi.fn(),
  getVaultHdReconciliation: vi.fn(),
  getVaultPubkey: vi.fn(),
  connectWorkbenchEvents: vi.fn(() => vi.fn()),
}));
const vault = vi.hoisted(() => ({
  status: 'locked' as string,
  refresh: vi.fn(),
  afterCreated: vi.fn(),
  sealValue: vi.fn(),
  sealHdWallet: vi.fn(),
}));
const popup = vi.hoisted(() => ({ blocked: false, opened: 0, close: vi.fn() }));
vi.mock('@/context/ApiContext', async (original) => ({ ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api }));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => vault, useProtectedHdSupported: () => true, useVaultSandboxWarm: () => true }));
vi.mock('@/lib/vaultSandboxClient', async (original) => ({
  ...await original<typeof import('@/lib/vaultSandboxClient')>(),
  openVaultAuthorizationWindow: () => {
    popup.opened += 1;
    return popup.blocked ? null : { id: 'window-1', close: popup.close };
  },
}));
vi.mock('./vault-protected-unlock', () => ({ VaultProtectedUnlock: () => <div role="note">protected-unlock</div> }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const p = en.vaults.hd.protected;
const NAME = 'COLD_WALLET';
const fill = (template: string, values: Record<string, string>) => template.replace(/\{\{(\w+)\}\}/g, (_m, key: string) => values[key]);

const sealed = { envelope: wallet.envelope, accounts: wallet.accounts, expectedAccount0: wallet.expected_account0, establishingVmk: false };
const snapshot = (over: Record<string, unknown> = {}) => ({
  name: NAME,
  secret_id: 'vlt_public_1',
  kind: 'keypair',
  protection: 'protected',
  signer_kind: 'hd-seed',
  bitcoin_profile: 'bitcoin-address-types-v1',
  envelope_sha256: wallet.envelope_sha256,
  accounts: wallet.accounts,
  expected_account0: wallet.expected_account0,
  ...over,
});

const AsOwner = ({ children }: { children: ReactNode }) => (
  <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: null, capabilities: OWNER_INSTANCE_CAPABILITIES }}>{children}</InstanceAuthorizationContext.Provider>
);

beforeEach(() => {
  vi.stubGlobal('isSecureContext', true);
  vault.status = 'locked';
  popup.blocked = false;
  popup.opened = 0;
  vault.sealHdWallet.mockResolvedValue(sealed);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); unblockHdWallet(NAME); });

const openProtected = async (source: 'generate' | 'import' = 'generate') => {
  const onCreated = vi.fn();
  render(<I18nextProvider i18n={i18n}><AsOwner><VaultSecretForm onCancel={vi.fn()} onCreated={onCreated} /></AsOwner></I18nextProvider>);
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.change(screen.getByRole('textbox', { name: `${en.vaults.dialog.name} ${en.vaults.dialog.nameHint}` }), { target: { value: NAME } });
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.hd.chainMnemonic }));
  if (source === 'import') fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${en.vaults.dialog.protectedProtection}`) }));
  return onCreated;
};
const launch = async (mode: 'create' | 'import' = 'create') => {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: mode === 'create' ? p.launchCreate : p.launchImport })); });
};

it('takes nothing secret on the page: no word, passphrase or check fields, and no ask switch', async () => {
  await openProtected();
  expect(screen.queryByRole('textbox', { name: en.vaults.hd.import.label })).toBeNull();
  expect(screen.queryByText(en.vaults.hd.passphrase.title)).toBeNull();
  expect(screen.queryByRole('switch', { name: en.vaults.hd.askAccount0 })).toBeNull();
  expect(screen.getByText(p.tierNote)).toBeTruthy();
  expect(screen.getByText(p.generateHint)).toBeTruthy();
});

it('creates: opens the window from the click, saves the opaque result, and offers it only after an exact match', async () => {
  const onCreated = await openProtected();
  api.createVaultSecret.mockResolvedValue({ ok: true, secret: {}, reconciliation: snapshot() });
  await launch();
  expect(popup.opened).toBe(1);
  expect(vault.sealHdWallet).toHaveBeenCalledWith(NAME, 'create', 'window-1');
  expect(popup.close).toHaveBeenCalled();
  expect(api.createVaultSecret).toHaveBeenCalledOnce();
  const payload = api.createVaultSecret.mock.calls[0][0] as Record<string, unknown>;
  expect(payload).toEqual({
    name: NAME, kind: 'keypair', signer_kind: 'hd-seed', protection: 'protected',
    sealed: wallet.envelope, accounts: wallet.accounts, expected_account0: wallet.expected_account0,
  });
  expect(await screen.findByText(fill(p.done.title, { name: NAME }))).toBeTruthy();
  expect(screen.getByText(p.done.recovery)).toBeTruthy();
  expect(screen.getByText(wallet.expected_account0.eth)).toBeTruthy();
  expect(vault.afterCreated).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: en.vaults.hd.done.finish }));
  expect(onCreated).toHaveBeenCalledWith(NAME, 'created');
});

it('imports with the same flow', async () => {
  await openProtected('import');
  api.createVaultSecret.mockResolvedValue({ ok: true, reconciliation: snapshot() });
  await launch('import');
  expect(vault.sealHdWallet).toHaveBeenCalledWith(NAME, 'import', 'window-1');
  expect(await screen.findByText(fill(p.done.title, { name: NAME }))).toBeTruthy();
});

it('sends the setup registration with the first protected secret', async () => {
  await openProtected();
  vault.sealHdWallet.mockResolvedValue({ ...sealed, establishingVmk: true, authzFactorRegistration: { challenge_id: 'c1', credential: { rawId: 'r', response: {} } } });
  api.createVaultSecret.mockResolvedValue({ ok: true, reconciliation: snapshot() });
  await launch();
  expect(api.createVaultSecret.mock.calls[0][0]).toEqual(expect.objectContaining({
    establishing_vmk: true, authz_factor_registration: { challenge_id: 'c1', credential: { rawId: 'r', response: {} } },
  }));
});

it.each([
  ['blocked', null, p.notice.blocked],
  ['cancelled', new VaultSandboxError('authorization_cancelled', 'x', true), p.notice.cancelled],
  ['closed', new VaultSandboxError('authorization_window_closed', 'x', true), p.notice.closed],
  ['timed out', new VaultSandboxError('authorization_window_timeout', 'x', true), p.notice.timeout],
  ['reset', new VaultSandboxError('sandbox_reset', 'x', true), p.notice.reset],
  ['refused', new VaultSandboxError('seal_hd_failed', 'boom'), p.notice.failed],
])('before anything is sent: %s says nothing was saved and can be tried again', async (_label, error, notice) => {
  await openProtected();
  if (error === null) popup.blocked = true;
  else vault.sealHdWallet.mockRejectedValue(error);
  await launch();
  expect(screen.getByText(notice)).toBeTruthy();
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  expect(api.getVaultHdReconciliation).not.toHaveBeenCalled();
  expect((screen.getByRole('button', { name: p.launchCreate }) as HTMLButtonElement).disabled).toBe(false);
});

it('a lost save reply is checked, never retried: unconfirmed until the exact wallet is found by name', async () => {
  await openProtected();
  api.createVaultSecret.mockRejectedValue(new TypeError('Failed to fetch'));
  api.getVaultHdReconciliation.mockRejectedValueOnce(new TypeError('Failed to fetch'));
  await launch();
  const unconfirmed = fill(p.notice.unconfirmed, { name: NAME });
  expect(await screen.findByText(unconfirmed)).toBeTruthy();
  expect(screen.queryByText(/nothing was saved/i)).toBeNull();
  expect(screen.queryByRole('button', { name: p.launchCreate })).toBeNull();
  // Check again: the wallet that was saved is exactly this one, so it is offered.
  api.getVaultHdReconciliation.mockResolvedValueOnce({ ok: true, reconciliation: snapshot() });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: p.checkAgain })); });
  expect(await screen.findByText(fill(p.done.title, { name: NAME }))).toBeTruthy();
  expect(api.createVaultSecret).toHaveBeenCalledOnce();
  expect(vault.sealHdWallet).toHaveBeenCalledOnce();
});

it('a refusal is shown, but the wallet stays unconfirmed and blocked: never "not saved", never a new one', async () => {
  await openProtected();
  api.createVaultSecret.mockResolvedValue({ ok: false, code: 'secret_exists', message: 'A secret with this name already exists' });
  // Another wallet holds the name: its snapshot does not match, so nothing is offered.
  api.getVaultHdReconciliation.mockResolvedValue({ ok: true, reconciliation: snapshot({ secret_id: 'vlt_other', envelope_sha256: wallet.reordered_envelope_sha256 }) });
  await launch();
  expect(await screen.findByText(fill(p.notice.unconfirmed, { name: NAME }))).toBeTruthy();
  expect(screen.getByText(fill(p.notice.refusal, { message: 'A secret with this name already exists' }))).toBeTruthy();
  expect(screen.queryByText(/nothing was saved/i)).toBeNull();
  expect(screen.queryByText(fill(p.done.title, { name: NAME }))).toBeNull();
  expect(vault.sealHdWallet).toHaveBeenCalledOnce();
});

it('a saved wallet that differs from the window result is not offered', async () => {
  await openProtected();
  api.createVaultSecret.mockResolvedValue({ ok: true, reconciliation: snapshot({ envelope_sha256: wallet.reordered_envelope_sha256 }) });
  await launch();
  expect(await screen.findByText(fill(p.notice.mismatch, { name: NAME }))).toBeTruthy();
  expect(screen.queryByText(fill(p.done.title, { name: NAME }))).toBeNull();
  expect(screen.queryByRole('button', { name: p.checkAgain })).toBeNull();
});

it('an unreadable save answer is checked by name, and only a complete match is offered', async () => {
  await openProtected();
  api.createVaultSecret.mockResolvedValue({ ok: true, reconciliation: { ...snapshot(), extra: true } });
  // First read: one row differs (not this wallet); second read: exactly this wallet.
  api.getVaultHdReconciliation
    .mockResolvedValueOnce({ ok: true, reconciliation: snapshot({ accounts: wallet.accounts.map((row, i) => (i === 19 ? { ...row, solana: '00'.repeat(32) } : row)) }) })
    .mockResolvedValueOnce({ ok: true, reconciliation: snapshot() });
  await launch();
  expect(await screen.findByText(fill(p.notice.unconfirmed, { name: NAME }))).toBeTruthy();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: p.checkAgain })); });
  expect(await screen.findByText(fill(p.done.title, { name: NAME }))).toBeTruthy();
});

it('shows the vault setup first when there is no protected vault yet', async () => {
  vault.status = 'needs-setup';
  await openProtected();
  expect(screen.getByText('protected-unlock')).toBeTruthy();
  expect((screen.getByRole('button', { name: p.launchCreate }) as HTMLButtonElement).disabled).toBe(true);
});

it('closing the dialog while the window is open closes the window, and a late result is never saved', async () => {
  let finish: (value: typeof sealed) => void = () => undefined;
  vault.sealHdWallet.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  await openProtected();
  await launch();
  expect(screen.getByText(p.notice.waiting)).toBeTruthy();
  cleanup();
  expect(popup.close).toHaveBeenCalled();
  await act(async () => { finish(sealed); });
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});

it('keeps an unconfirmed or mismatched wallet blocked outside the dialog, but not a name another record holds', async () => {
  // Lost answer: whatever is under the name may be unconfirmed, so it stays blocked from funding.
  await openProtected();
  api.createVaultSecret.mockRejectedValue(new TypeError('Failed to fetch'));
  api.getVaultHdReconciliation.mockRejectedValue(new TypeError('Failed to fetch'));
  await launch();
  await screen.findByText(fill(p.notice.unconfirmed, { name: NAME }));
  expect(isHdWalletBlocked(NAME)).toBe(true);
  // A later exact match lifts it.
  api.getVaultHdReconciliation.mockResolvedValueOnce({ ok: true, reconciliation: snapshot() });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: p.checkAgain })); });
  await screen.findByText(fill(p.done.title, { name: NAME }));
  expect(isHdWalletBlocked(NAME)).toBe(false);
  cleanup();

  // The saved record differs from the window's result: blocked, never deleted.
  await openProtected();
  api.createVaultSecret.mockResolvedValue({ ok: true, reconciliation: snapshot({ envelope_sha256: wallet.reordered_envelope_sha256 }) });
  await launch();
  await screen.findByText(fill(p.notice.mismatch, { name: NAME }));
  expect(isHdWalletBlocked(NAME)).toBe(true);
  unblockHdWallet(NAME);
  cleanup();

  // Refused because the name is taken: that other record is not this browser's to block.
  await openProtected();
  api.createVaultSecret.mockResolvedValue({ ok: false, code: 'secret_exists', message: 'exists' });
  api.getVaultHdReconciliation.mockResolvedValue({ ok: false, code: 'invalid_hd_wallet' });
  await launch();
  await screen.findByText(fill(p.notice.unconfirmed, { name: NAME }));
  expect(isHdWalletBlocked(NAME)).toBe(false);
});
