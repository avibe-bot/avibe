/* @vitest-environment jsdom */

// The name check in the Add key dialog (owner, 2026-11-15): a taken name is said on leaving the field
// and again before anything is made, a protected seed wallet refused for its name says it was not
// saved, the tier chosen stays across a failed save and a reopened dialog, and a disabled Create says
// when only the name is missing.
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
  listVaultSecrets: vi.fn(async (_opts?: { cache?: boolean }) => ({ ok: true, secrets: [] as Array<{ name: string }> })),
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
const popup = vi.hoisted(() => ({ opened: 0, close: vi.fn() }));
const sandbox = vi.hoisted(() => ({ supported: true as boolean | null, warm: true }));
vi.mock('@/context/ApiContext', async (original) => ({ ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api }));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => vault, useProtectedHdSupported: () => sandbox.supported, useVaultSandboxWarm: () => sandbox.warm }));
vi.mock('@/lib/vaultSandboxClient', async (original) => ({
  ...await original<typeof import('@/lib/vaultSandboxClient')>(),
  openVaultAuthorizationWindow: () => {
    popup.opened += 1;
    return { id: 'window-1', close: popup.close };
  },
}));
// These tests cover the protected seed wallet itself, so its entry is on here.
vi.mock('@/lib/vaultProtectedSeedSwitch', () => ({ PROTECTED_SEED_WALLET_ENABLED: true }));
vi.mock('./vault-protected-unlock', () => ({ VaultProtectedUnlock: () => <div role="note">protected-unlock</div> }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const d = en.vaults.dialog;
const hd = en.vaults.hd;
const p = hd.protected;
const NAME = 'COLD_WALLET';
const sealed = { envelope: wallet.envelope, accounts: wallet.accounts, expectedAccount0: wallet.expected_account0, establishingVmk: false };
const snapshot = (over: Record<string, unknown> = {}) => ({
  name: NAME, secret_id: 'vlt_other', kind: 'keypair', protection: 'protected', signer_kind: 'hd-seed',
  bitcoin_profile: 'bitcoin-address-types-v1', envelope_sha256: wallet.reordered_envelope_sha256,
  accounts: wallet.accounts, expected_account0: wallet.expected_account0, ...over,
});
const otherAccount0 = Object.fromEntries(Object.entries(wallet.expected_account0).map(([key, value]) => [key, `${value}x`]));

const AsOwner = ({ children }: { children: ReactNode }) => (
  <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: null, capabilities: OWNER_INSTANCE_CAPABILITIES }}>{children}</InstanceAuthorizationContext.Provider>
);
const mount = () => render(<I18nextProvider i18n={i18n}><AsOwner><VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} /></AsOwner></I18nextProvider>);
const nameBox = () => screen.getByRole('textbox', { name: new RegExp(`^${d.name}`) });
const protectedCard = () => screen.getByRole('button', { name: new RegExp(`^${d.protectedProtection}`) });
const wordsInput = () => screen.queryByRole('textbox', { name: hd.import.label });
const taken = (...names: string[]) => api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: names.map((name) => ({ name })) });

const openWallet = async (source: 'generate' | 'import' = 'generate', tier: 'protected' | 'standard' = 'protected') => {
  mount();
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: d.kindKeypair }));
  fireEvent.change(nameBox(), { target: { value: NAME } });
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  if (tier === 'protected') fireEvent.click(protectedCard());
  if (source === 'import') fireEvent.click(screen.getByRole('radio', { name: d.signingImport }));
};

beforeEach(() => {
  vi.stubGlobal('isSecureContext', true);
  sessionStorage.clear();
  vault.status = 'locked';
  sandbox.supported = true;
  sandbox.warm = true;
  taken();
  api.listDependencies.mockResolvedValue({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.12' }] });
  popup.opened = 0;
  vault.sealHdWallet.mockResolvedValue(sealed);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); unblockHdWallet(NAME); });

it('says a taken name on leaving the field, and Create waits for another name', async () => {
  taken(NAME);
  await openWallet('generate', 'standard');
  await act(async () => { fireEvent.blur(nameBox()); });
  expect(await screen.findByText(d.nameTaken)).toBeTruthy();
  expect(nameBox().getAttribute('aria-invalid')).toBe('true');
  expect((screen.getByRole('button', { name: hd.generateCta }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(nameBox(), { target: { value: 'COLD_WALLET_2' } });
  expect(screen.queryByText(d.nameTaken)).toBeNull();
  expect((screen.getByRole('button', { name: hd.generateCta }) as HTMLButtonElement).disabled).toBe(false);
});

it('names the existing key when the name differs from it only in letter case', async () => {
  taken('cold_wallet');
  await openWallet('generate', 'standard');
  await act(async () => { fireEvent.blur(nameBox()); });
  expect(await screen.findByText(d.nameCaseConflict.replace('{{name}}', 'cold_wallet'))).toBeTruthy();
});

it('checks again, uncached, before the words are made: a name taken since leaving the field shows no words', async () => {
  await openWallet('generate', 'standard');
  await act(async () => { fireEvent.blur(nameBox()); });
  taken(NAME);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.generateCta })); });
  expect(await screen.findByText(d.nameTaken)).toBeTruthy();
  expect(api.listVaultSecrets).toHaveBeenLastCalledWith({ cache: false });
  expect(screen.queryByRole('list', { name: hd.steps.backup })).toBeNull();
  expect(screen.queryByRole('button', { name: hd.backup.show.replace('{{count}}', '12') })).toBeNull();
});

it('a failed name read blocks nothing: the server refuses a taken name at save', async () => {
  api.listVaultSecrets.mockRejectedValue(new Error('offline'));
  await openWallet('generate', 'standard');
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.generateCta })); });
  expect(await screen.findByRole('button', { name: hd.backup.show.replace('{{count}}', '12') })).toBeTruthy();
});

it('protected: a name taken just before launch opens nothing in the window and makes no wallet', async () => {
  await openWallet('generate');
  taken(NAME);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: p.launchCreate })); });
  expect(await screen.findByText(d.nameTaken)).toBeTruthy();
  // The window was reserved by the click (it must be), then closed before anything was made in it.
  expect(popup.opened).toBe(1);
  expect(popup.close).toHaveBeenCalled();
  expect(vault.sealHdWallet).not.toHaveBeenCalled();
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  expect((screen.getByRole('button', { name: p.launchCreate }) as HTMLButtonElement).disabled).toBe(true);
});

it.each(['secret_exists', 'secret_name_case_conflict'])('protected create refused for its name (%s): not saved, new words next time, still Protected', async (code) => {
  await openWallet('generate');
  api.createVaultSecret.mockResolvedValue({ ok: false, code, message: 'taken' });
  api.getVaultHdReconciliation.mockResolvedValue({ ok: false, code: 'invalid_hd_wallet' });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: p.launchCreate })); });
  expect(await screen.findByText(p.nameTakenCreate)).toBeTruthy();
  expect(isHdWalletBlocked(NAME)).toBe(false);
  // The tier stays Protected and the main page has no words box, before and after switching to Import.
  expect(protectedCard().getAttribute('aria-pressed')).toBe('true');
  expect(wordsInput()).toBeNull();
  fireEvent.click(screen.getByRole('radio', { name: d.signingImport }));
  expect(protectedCard().getAttribute('aria-pressed')).toBe('true');
  expect(wordsInput()).toBeNull();
  fireEvent.click(screen.getByRole('radio', { name: d.signingGenerate }));
  // The name is editable again, and launching waits for a different one.
  expect((nameBox() as HTMLInputElement).disabled).toBe(false);
  expect((screen.getByRole('button', { name: p.launchCreate }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(nameBox(), { target: { value: 'COLD_WALLET_2' } });
  expect((screen.getByRole('button', { name: p.launchCreate }) as HTMLButtonElement).disabled).toBe(false);
});

it('protected import refused because another seed wallet holds the name: not saved, import the same words again', async () => {
  await openWallet('import');
  api.createVaultSecret.mockResolvedValue({ ok: false, code: 'secret_exists', message: 'taken' });
  api.getVaultHdReconciliation.mockResolvedValue({ ok: true, reconciliation: snapshot({ expected_account0: otherAccount0 }) });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: p.launchImport })); });
  expect(await screen.findByText(p.nameTakenImport)).toBeTruthy();
  expect(protectedCard().getAttribute('aria-pressed')).toBe('true');
  expect(wordsInput()).toBeNull();
});

it('protected import refused where the name\'s record can\'t be read: unconfirmed, never "not saved"', async () => {
  await openWallet('import');
  api.createVaultSecret.mockResolvedValue({ ok: false, code: 'secret_exists', message: 'taken' });
  api.getVaultHdReconciliation.mockRejectedValue(new Error('offline'));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: p.launchImport })); });
  expect(await screen.findByText(p.notice.unconfirmed.replace('{{name}}', NAME))).toBeTruthy();
  expect(screen.queryByText(p.nameTakenImport)).toBeNull();
});

it.each([
  ['secret_exists', 'A secret with this name already exists'],
  ['invalid_protected_authz', 'protected operation challenge expired'],
])('a failed protected save (%s) and a reopened dialog both stay Protected', async (code, message) => {
  await openWallet('generate');
  api.createVaultSecret.mockResolvedValue({ ok: false, code, message });
  api.getVaultHdReconciliation.mockResolvedValue({ ok: false, code: 'secret_not_found' });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: p.launchCreate })); });
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(protectedCard().getAttribute('aria-pressed')).toBe('true');
  expect(wordsInput()).toBeNull();
  cleanup();
  unblockHdWallet(NAME);
  // Reopened: the tier chosen last is kept, and Import shows no words box on the page.
  mount();
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: d.kindKeypair }));
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  expect(protectedCard().getAttribute('aria-pressed')).toBe('true');
  fireEvent.click(screen.getByRole('radio', { name: d.signingImport }));
  expect(wordsInput()).toBeNull();
  expect(screen.queryByText(hd.standardTierLine)).toBeNull();
});

it('remembers Standard too, and a dialog for an Agent request keeps the tier it asks for', async () => {
  await openWallet('generate');
  fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${d.standardProtection}`) }));
  cleanup();
  mount();
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  expect(protectedCard().getAttribute('aria-pressed')).toBe('false');
  cleanup();
  sessionStorage.clear();
  await openWallet('generate');
  cleanup();
  render(<I18nextProvider i18n={i18n}><AsOwner><VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} requestSpec={{ protection: 'standard' }} /></AsOwner></I18nextProvider>);
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  expect(protectedCard().getAttribute('aria-pressed')).toBe('false');
});

it('says when only the name is missing, by Create', async () => {
  mount();
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  expect(screen.getByText(d.nameRequired)).toBeTruthy();
  fireEvent.click(screen.getByRole('radio', { name: d.kindKeypair }));
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  expect(screen.getByText(d.nameRequiredWallet)).toBeTruthy();
  fireEvent.change(nameBox(), { target: { value: NAME } });
  expect(screen.queryByText(d.nameRequiredWallet)).toBeNull();
});

it('a name taken while the words were being backed up: the last Create returns to the first step with the same words', async () => {
  await openWallet('generate', 'standard');
  fireEvent.click(screen.getByRole('button', { name: hd.generateCta }));
  fireEvent.click(await screen.findByRole('button', { name: hd.backup.show.replace('{{count}}', '12') }));
  const readWords = () => Array.from(screen.getByRole('list', { name: hd.steps.backup }).querySelectorAll('li')).map((item) => item.querySelectorAll('span')[1].textContent ?? '');
  const words = readWords();
  fireEvent.click(screen.getByRole('button', { name: hd.backup.done }));
  for (const group of screen.getAllByRole('radiogroup')) {
    const position = Number(/\d+/.exec(group.getAttribute('aria-label') ?? '')![0]);
    fireEvent.click(screen.getAllByRole('radio', { name: words[position - 1] }).find((radio) => group.contains(radio))!);
  }
  taken(NAME);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.createCta })); });
  expect(await screen.findByText(hd.nameTakenAfterSave)).toBeTruthy();
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  fireEvent.change(nameBox(), { target: { value: 'COLD_WALLET_2' } });
  fireEvent.click(screen.getByRole('button', { name: hd.generateCta }));
  fireEvent.click(await screen.findByRole('button', { name: hd.backup.show.replace('{{count}}', '12') }));
  expect(readWords()).toEqual(words);
});

it('is busy while the name is checked: Create and Cancel wait, and a closed dialog saves nothing', async () => {
  let answer: (value: { ok: boolean; secrets: Array<{ name: string }> }) => void = () => {};
  mount();
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.change(nameBox(), { target: { value: 'API_TOKEN' } });
  fireEvent.change(screen.getByPlaceholderText(d.valuePlaceholder), { target: { value: 'token' } });
  api.listVaultSecrets.mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
  const readsBefore = api.listVaultSecrets.mock.calls.length;
  const create = screen.getByRole('button', { name: d.createSecret }) as HTMLButtonElement;
  await act(async () => { fireEvent.click(create); });
  expect(create.disabled).toBe(true);
  expect((screen.getByRole('button', { name: d.cancel }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(create);
  cleanup();
  await act(async () => { answer({ ok: true, secrets: [] }); });
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  // The second click did not start another check (or save).
  expect(api.listVaultSecrets.mock.calls.length - readsBefore).toBe(1);
});

it('says one reason by a disabled Create at a time, in the order the form is filled in', async () => {
  mount();
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  // A static secret: the name, then the value.
  expect(screen.getByText(d.nameRequired)).toBeTruthy();
  fireEvent.change(nameBox(), { target: { value: 'API_TOKEN' } });
  expect(screen.queryByText(d.nameRequired)).toBeNull();
  expect(screen.getByText(d.createNeedsValue)).toBeTruthy();
  fireEvent.change(screen.getByPlaceholderText(d.valuePlaceholder), { target: { value: 'token' } });
  expect(screen.queryByText(d.createNeedsValue)).toBeNull();
  // Protected, locked: the tier must be unlocked first.
  fireEvent.click(protectedCard());
  expect(screen.getByText(d.createNeedsProtected)).toBeTruthy();
  // A signing key to import: the key.
  fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${d.standardProtection}`) }));
  fireEvent.click(screen.getByRole('radio', { name: d.kindKeypair }));
  fireEvent.click(screen.getByRole('radio', { name: d.signingImport }));
  expect(screen.getByText(d.createNeedsKey)).toBeTruthy();
  // A seed wallet to import on Standard: the words; a word problem is said by the words box, not here.
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  fireEvent.click(screen.getByRole('radio', { name: d.signingImport }));
  expect(screen.getByText(d.createNeedsWords)).toBeTruthy();
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: 'abandon abandon' } });
  expect(screen.queryByText(d.createNeedsWords)).toBeNull();
});

it('says to update avault when it is what Create waits for', async () => {
  api.listDependencies.mockResolvedValue({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.0' }] });
  await openWallet('generate', 'standard');
  expect(await screen.findByText(d.createNeedsAvault)).toBeTruthy();
});

it('an older, slower name check does not clear a newer one', async () => {
  await openWallet('generate', 'standard');
  let late: (value: { ok: boolean; secrets: Array<{ name: string }> }) => void = () => {};
  api.listVaultSecrets.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
  await act(async () => { fireEvent.blur(nameBox()); });
  taken(NAME);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.generateCta })); });
  expect(await screen.findByText(d.nameTaken)).toBeTruthy();
  await act(async () => { late({ ok: true, secrets: [] }); });
  expect(screen.getByText(d.nameTaken)).toBeTruthy();
});

it('a handoff that asks for Protected is not lowered by a tier chosen before', async () => {
  sessionStorage.setItem('avibe.vault.addTier', 'standard');
  render(<I18nextProvider i18n={i18n}><AsOwner><VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} defaultProtection="protected" /></AsOwner></I18nextProvider>);
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  expect(protectedCard().getAttribute('aria-pressed')).toBe('true');
});

it('adds no reason where a field already says what is wrong, or where nothing can be done yet', async () => {
  mount();
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.change(nameBox(), { target: { value: 'API_TOKEN' } });
  // A file source with no file: the file picker is the step, not "enter a value".
  fireEvent.click(screen.getByRole('radio', { name: 'File' }));
  expect(screen.queryByText(d.createNeedsValue)).toBeNull();
  // Field rows: a row whose name is wrong says so itself, even with a value left empty.
  fireEvent.click(screen.getByRole('radio', { name: 'Text' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.add }));
  fireEvent.change(screen.getByLabelText('Field 1 name'), { target: { value: '1bad' } });
  expect(screen.queryByText(d.createNeedsValue)).toBeNull();
  fireEvent.change(screen.getByLabelText('Field 1 name'), { target: { value: 'username' } });
  screen.queryAllByLabelText(/^Field \d+ name$/).slice(1).forEach((input, index) => fireEvent.change(input, { target: { value: `field_${index + 2}` } }));
  expect(screen.getByText(d.createNeedsValue)).toBeTruthy();
  cleanup();

  // A pasted key that doesn't parse: the key box says so; no "paste the key" beside Create.
  mount();
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.change(nameBox(), { target: { value: 'SIGNER' } });
  fireEvent.click(screen.getByRole('radio', { name: d.kindKeypair }));
  fireEvent.click(screen.getByRole('radio', { name: d.signingImport }));
  expect(screen.getByText(d.createNeedsKey)).toBeTruthy();
  fireEvent.change(screen.getByRole('textbox', { name: d.signingImport }), { target: { value: 'not-a-key' } });
  expect(screen.getByText(en.vaults.dialog.errors.invalidPrivateKey)).toBeTruthy();
  expect(screen.queryByText(d.createNeedsKey)).toBeNull();
  cleanup();

  // A protected seed wallet while the secure window's code still loads, a locked vault being enough.
  sandbox.supported = null;
  sandbox.warm = false;
  await openWallet('generate');
  expect(screen.queryByText(d.createNeedsProtected)).toBeNull();
  cleanup();
  // ...or while the vault is still being checked, or in error (its panel speaks).
  for (const status of ['checking', 'error']) {
    vault.status = status;
    await openWallet('generate');
    expect(screen.queryByText(d.createNeedsProtected)).toBeNull();
    cleanup();
  }
  // No vault yet: the panel above sets one up, and the reason points there.
  vault.status = 'needs-setup';
  sandbox.supported = true;
  sandbox.warm = true;
  await openWallet('generate');
  expect(screen.getByText(d.createNeedsProtected)).toBeTruthy();
});
