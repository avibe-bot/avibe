/* @vitest-environment jsdom */

// Acceptance item 2 (owner, 2026-11-12): the words shown are the words sealed. These tests drive the
// real Add key dialog the way a person does, read the words from the screen, and check that the one
// thing that leaves the browser (the seed in the blind box) is exactly BIP39(displayed words), that
// no words, seed or accounts are posted, and that a wallet whose stored #0 differs is never offered.
import { webcrypto } from 'node:crypto';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { bytesToHex } from '@noble/hashes/utils.js';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import en from '@/i18n/en.json';
import type { VaultHdAccount } from '@/context/ApiContext';
import { deriveAccounts, wordsToSeed } from '@/lib/vaultHdWallet';
import { VaultSecretForm } from './vault-secret-form';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import type { ReactNode } from 'react';

let canManage = true;
const AsRole = ({ children }: { children: ReactNode }) => (
  <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: null, capabilities: canManage ? OWNER_INSTANCE_CAPABILITIES : { ...OWNER_INSTANCE_CAPABILITIES, can_manage_instance: false } }}>{children}</InstanceAuthorizationContext.Provider>
);


const api = vi.hoisted(() => ({
  listDependencies: vi.fn(),
  listVaultSecrets: vi.fn(async () => ({ secrets: [] })),
  listSkills: vi.fn(async () => ({ skills: [] })),
  createVaultSecret: vi.fn(),
  updateVaultHdAccount: vi.fn(async () => ({ ok: true })),
  deleteVaultSecret: vi.fn(async () => ({ ok: true, removed: true })),
  getVaultPubkey: vi.fn(async () => ({ public_key: 'public', fingerprint: 'fingerprint' })),
  deriveSigningAddresses: vi.fn(async () => ({ ok: true, addresses: {} })),
  connectWorkbenchEvents: vi.fn(() => vi.fn()),
}));
const blindBox = vi.hoisted(() => ({ scheme: 'test', enc: 'enc', ct: 'ciphertext' }));
// The plaintext the dialog hands to the sealer, copied before the dialog zeroes it.
const sealed = vi.hoisted(() => ({ plaintext: null as Uint8Array | null, context: null as unknown }));
const sealBlindBox = vi.hoisted(() => vi.fn(async (plaintext: Uint8Array, _key: unknown, context: unknown) => {
  sealed.plaintext = Uint8Array.from(plaintext);
  sealed.context = context;
  return blindBox;
}));
vi.mock('@/context/ApiContext', async (original) => ({ ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api }));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => ({ status: 'locked', refresh: vi.fn(), afterCreated: vi.fn(), sealValue: vi.fn() }) }));
vi.mock('./vault-protected-unlock', () => ({ VaultProtectedUnlock: () => null }));
vi.mock('./avault-authorization-notice', () => ({ AvaultAuthorizationNotice: () => <div role="note">avault-authorization</div> }));
vi.mock('@/lib/vaultCrypto', async (original) => ({ ...await original<typeof import('@/lib/vaultCrypto')>(), sealBlindBox }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const hd = en.vaults.hd;
const OFFICIAL = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

const storedFor = (words: string[], passphrase = ''): VaultHdAccount => {
  const seed = wordsToSeed(words, passphrase);
  const a = deriveAccounts(seed, 1)[0];
  return { account: 0, name: null, eth: a.eth, btc_taproot: a.btc_taproot, solana: a.solana, paths: a.paths, always_ask: false, last_signer: null };
};

beforeEach(async () => {
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('isSecureContext', true);
  sealed.plaintext = null;
  api.listDependencies.mockResolvedValue({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.11' }] });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

const openWallet = async (name = 'AGENT_WALLET') => {
  const onCreated = vi.fn();
  render(<I18nextProvider i18n={i18n}><AsRole><VaultSecretForm onCancel={vi.fn()} onCreated={onCreated} /></AsRole></I18nextProvider>);
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.change(screen.getByRole('textbox', { name: `${en.vaults.dialog.name} ${en.vaults.dialog.nameHint}` }), { target: { value: name } });
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  return onCreated;
};
const postedPayload = () => api.createVaultSecret.mock.calls[0][0] as Record<string, unknown>;
const expectNoSecretsPosted = (words: string[]) => {
  const body = JSON.stringify(api.createVaultSecret.mock.calls);
  // The phrase, any two adjacent words, or a word as a value: none may be posted.
  expect(body).not.toContain(words.join(' '));
  for (let i = 1; i < words.length; i += 1) expect(body).not.toContain(`${words[i - 1]} ${words[i]}`);
  // No posted string value is a word of the phrase (keys such as "scheme" may coincide with one).
  const values: string[] = [];
  const walk = (value: unknown) => {
    if (typeof value === 'string') values.push(value);
    else if (Array.isArray(value)) value.forEach(walk);
    else if (value && typeof value === 'object') Object.values(value).forEach(walk);
  };
  walk(api.createVaultSecret.mock.calls);
  for (const word of new Set(words)) expect(values).not.toContain(word);
  expect(body).not.toContain(bytesToHex(sealed.plaintext!));
  expect(postedPayload()).toEqual(expect.objectContaining({
    kind: 'keypair', protection: 'standard', signer_kind: 'hd-seed',
    keypair: { type: 'hd-seed', source: 'import', format: 'raw-seed' }, blind_box: blindBox,
  }));
  for (const field of ['accounts', 'public_meta', 'policy', 'sealed']) expect(postedPayload()).not.toHaveProperty(field);
};

it('creates a wallet: the sealed seed is BIP39 of the words the screen showed, and #0 is checked before it is offered', async () => {
  const onCreated = await openWallet();
  fireEvent.click(screen.getByRole('button', { name: hd.generateCta }));
  // The words are hidden until asked for, then read off the screen as a person writes them down.
  fireEvent.click(await screen.findByRole('button', { name: hd.backup.show.replace('{{count}}', '12') }));
  const grid = screen.getByRole('list', { name: hd.steps.backup });
  const words = within(grid).getAllByRole('listitem').map((item) => item.querySelectorAll('span')[1].textContent ?? '');
  expect(words).toHaveLength(12);
  fireEvent.click(screen.getByRole('button', { name: hd.backup.done }));
  // Answer the three questions from the paper copy.
  for (const group of screen.getAllByRole('radiogroup')) {
    const position = Number(/\d+/.exec(group.getAttribute('aria-label') ?? '')![0]);
    fireEvent.click(within(group).getByRole('radio', { name: words[position - 1] }));
  }
  api.createVaultSecret.mockResolvedValue({ ok: true, secret: { accounts: [storedFor(words)] } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.createCta })); });
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(bytesToHex(sealed.plaintext!)).toBe(bytesToHex(wordsToSeed(words)));
  expect(sealed.context).toEqual({ purpose: 'keypair-import', name: 'AGENT_WALLET', curve: 'hd-seed', format: 'raw-seed' });
  expectNoSecretsPosted(words);
  // Done: #0's addresses, the ones the words give, marked checked.
  expect(await screen.findByText(hd.done.verified)).toBeTruthy();
  expect(screen.getByText(storedFor(words).eth)).toBeTruthy();
  // The words are gone from the page.
  expect(screen.queryByText(words[0], { selector: 'span.font-mono' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: hd.done.finish }));
  expect(onCreated).toHaveBeenCalledWith('AGENT_WALLET', 'created');
});

it('refuses to create while a check answer is wrong, and says which word', async () => {
  await openWallet();
  fireEvent.click(screen.getByRole('button', { name: hd.generateCta }));
  fireEvent.click(await screen.findByRole('button', { name: hd.backup.show.replace('{{count}}', '12') }));
  fireEvent.click(screen.getByRole('button', { name: hd.backup.done }));
  const group = screen.getAllByRole('radiogroup')[0];
  const options = within(group).getAllByRole('radio');
  // Pick an option, then whichever of the three is not the answer.
  const position = Number(/\d+/.exec(group.getAttribute('aria-label') ?? '')![0]);
  fireEvent.click(screen.getByRole('button', { name: hd.check.again }));
  // Back on the words, which are hidden again until asked for.
  fireEvent.click(screen.getByRole('button', { name: hd.backup.show.replace('{{count}}', '12') }));
  const words = within(screen.getByRole('list', { name: hd.steps.backup })).getAllByRole('listitem').map((i) => i.querySelectorAll('span')[1].textContent ?? '');
  fireEvent.click(screen.getByRole('button', { name: hd.backup.done }));
  const again = screen.getAllByRole('radiogroup')[0];
  const wrong = within(again).getAllByRole('radio').find((o) => o.textContent !== words[position - 1])!;
  fireEvent.click(wrong);
  expect(screen.getByRole('alert').textContent).toContain(hd.check.wrong.replace('{{position}}', String(position)));
  expect((screen.getByRole('button', { name: hd.createCta }) as HTMLButtonElement).disabled).toBe(true);
  expect(options).toHaveLength(3);
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});

it.each([
  ['', storedFor(OFFICIAL.split(' '))],
  ['TREZOR', storedFor(OFFICIAL.split(' '), 'TREZOR')],
])('imports a phrase (passphrase %j): previews #0, seals exactly that seed, and checks #0 after saving', async (passphrase, expected) => {
  await openWallet();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  // Typed messily: capitals and extra spaces are the same words.
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: `  ${OFFICIAL.toUpperCase().replace(/ /g, '  ')} ` } });
  expect(screen.getByText(hd.import.ok.replace('{{count}}', '12'))).toBeTruthy();
  if (passphrase) {
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${hd.import.passphrase.replace(/[()]/g, '\\$&')}`) }));
    fireEvent.change(screen.getByLabelText(hd.import.passphrase), { target: { value: passphrase } });
  }
  // The preview is #0 for these words and this passphrase.
  expect(screen.getByText(expected.eth)).toBeTruthy();
  expect(screen.getByText(expected.solana)).toBeTruthy();
  api.createVaultSecret.mockResolvedValue({ ok: true, secret: { accounts: [expected] } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(bytesToHex(sealed.plaintext!)).toBe(bytesToHex(wordsToSeed(OFFICIAL.split(' '), passphrase)));
  expectNoSecretsPosted(OFFICIAL.split(' '));
  if (passphrase) expect(JSON.stringify(api.createVaultSecret.mock.calls)).not.toContain(passphrase);
  expect(await screen.findByText(hd.done.verified)).toBeTruthy();
});

it.each([
  ['eth'], ['btc_taproot'], ['solana'], ['missing #0'],
] as const)('fails closed when the stored #0 differs (%s): deletes it, logs by name, reports no success', async (field) => {
  const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {});
  localStorage.clear();
  const onCreated = await openWallet();
  const onCancel = vi.fn();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  const other = storedFor('legal winner thank year wave sausage worth useful legal winner thank yellow'.split(' '));
  const own = storedFor(OFFICIAL.split(' '));
  const accounts = field === 'missing #0' ? [{ ...own, account: 1 }] : [{ ...own, [field]: other[field] }];
  api.createVaultSecret.mockResolvedValue({ ok: true, secret: { accounts } });
  api.deleteVaultSecret.mockResolvedValueOnce({ ok: false, code: 'boom', message: 'nope' } as never);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  expect(await screen.findByText(hd.mismatch)).toBeTruthy();
  expect(screen.queryByText(hd.done.verified)).toBeNull();
  expect(api.updateVaultHdAccount).not.toHaveBeenCalled();
  expect(errorLog).toHaveBeenCalledWith(expect.stringContaining('address check failed'), { name: 'AGENT_WALLET' });
  // The automatic delete was refused: the wallet stays blocked in this browser, with no way to "finish".
  expect(api.deleteVaultSecret).toHaveBeenCalledWith('AGENT_WALLET');
  expect(JSON.parse(localStorage.getItem('avibe.vault.hdAddressCheckFailed') ?? '[]').map((b: { name: string }) => b.name)).toEqual(['AGENT_WALLET']);
  expect(screen.queryByRole('button', { name: hd.done.finish })).toBeNull();
  // Deleting again succeeds: unblocked, and closing reports nothing created.
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.deleteWallet })); });
  expect(screen.getByText(hd.mismatchDeleted)).toBeTruthy();
  expect(localStorage.getItem('avibe.vault.hdAddressCheckFailed')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: hd.close }));
  expect(onCreated).not.toHaveBeenCalled();
  void onCancel;
});

it('drops the phrase, passphrase and check when switching to a static value', async () => {
  await openWallet();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${hd.import.passphrase.replace(/[()]/g, '\\$&')}`) }));
  fireEvent.change(screen.getByLabelText(hd.import.passphrase), { target: { value: 'TREZOR' } });
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindStatic }));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  expect((screen.getByRole('textbox', { name: hd.import.label }) as HTMLTextAreaElement).value).toBe('');
  expect(screen.queryByDisplayValue('TREZOR')).toBeNull();
});

it('names an unknown word and offers list words; refuses a bad checksum; never saves either', async () => {
  await openWallet();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  const box = screen.getByRole('textbox', { name: hd.import.label });
  fireEvent.change(box, { target: { value: OFFICIAL.replace('about', 'abuot') } });
  expect(screen.getByRole('alert').textContent).toContain(hd.import.unknown.replace('{{position}}', '12').replace('{{word}}', 'abuot'));
  expect((screen.getByRole('button', { name: hd.importCta }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(box, { target: { value: OFFICIAL.replace('about', 'abandon') } });
  expect(screen.getByRole('alert').textContent).toContain(hd.import.checksum.replace('{{count}}', '12'));
  expect((screen.getByRole('button', { name: hd.importCta }) as HTMLButtonElement).disabled).toBe(true);
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});

it('keeps what was typed and says to update when avault is too old', async () => {
  api.listDependencies.mockResolvedValue({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.10' }] });
  await openWallet();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  expect(await screen.findByText(hd.avaultOld.replace('{{version}}', '0.1.11').replace('{{installed}}', '0.1.10'))).toBeTruthy();
  expect((screen.getByRole('button', { name: hd.importCta }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('textbox', { name: hd.import.label }) as HTMLTextAreaElement).value).toBe(OFFICIAL);
});

it('saves #0 asking before signing in the create itself, with no later PATCH and no window without it', async () => {
  api.updateVaultHdAccount.mockClear();
  await openWallet();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  fireEvent.click(screen.getByRole('switch', { name: hd.askAccount0 }));
  api.createVaultSecret.mockResolvedValue({ ok: true, secret: { accounts: [{ ...storedFor(OFFICIAL.split(' ')), always_ask: true }] } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  await screen.findByRole('button', { name: hd.done.finish });
  expect(postedPayload().account0_always_ask).toBe(true);
  expect(postedPayload()).not.toHaveProperty('policy');
  expect(api.updateVaultHdAccount).not.toHaveBeenCalled();
  expect((screen.getByRole('button', { name: hd.done.finish }) as HTMLButtonElement).disabled).toBe(false);
});

it('leaves account0_always_ask out of the create when the switch is off', async () => {
  await openWallet();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  api.createVaultSecret.mockResolvedValue({ ok: true, secret: { accounts: [storedFor(OFFICIAL.split(' '))] } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  await screen.findByRole('button', { name: hd.done.finish });
  // undefined is dropped on the wire, like the other optional fields.
  expect(JSON.parse(JSON.stringify(postedPayload()))).not.toHaveProperty('account0_always_ask');
});

it('falls back to turning it on if the server ignored it, and does not report done until it sticks', async () => {
  await openWallet();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  fireEvent.click(screen.getByRole('switch', { name: hd.askAccount0 }));
  api.createVaultSecret.mockResolvedValue({ ok: true, secret: { accounts: [storedFor(OFFICIAL.split(' '))] } });
  // The first attempt is refused, the retry loses the connection, the third sticks.
  api.updateVaultHdAccount.mockResolvedValueOnce({ ok: false, code: 'boom', message: 'nope' } as never);
  api.updateVaultHdAccount.mockRejectedValueOnce(new TypeError('Failed to fetch'));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  expect(await screen.findByText(hd.askFailed)).toBeTruthy();
  expect((screen.getByRole('button', { name: hd.done.finish }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.askRetry })); });
  expect(screen.getByText(hd.askFailed)).toBeTruthy();
  expect((screen.getByRole('button', { name: hd.done.finish }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.askRetry })); });
  expect(api.updateVaultHdAccount).toHaveBeenCalledTimes(3);
  expect(screen.queryByText(hd.askFailed)).toBeNull();
  expect((screen.getByRole('button', { name: hd.done.finish }) as HTMLButtonElement).disabled).toBe(false);
  expect(api.createVaultSecret).toHaveBeenCalledOnce();
});

it('shows the avault authorization notice when saving a wallet needs it, keeping the input', async () => {
  await openWallet();
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  api.createVaultSecret.mockRejectedValue(Object.assign(new Error('needs keychain'), { code: 'avault_authorization_required' }));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  expect(await screen.findByText('avault-authorization')).toBeTruthy();
  expect((screen.getByRole('textbox', { name: hd.import.label }) as HTMLTextAreaElement).value).toBe(OFFICIAL);
});

it('is not blocked by proxy settings left in Advanced before choosing a seed-phrase wallet', async () => {
  render(<I18nextProvider i18n={i18n}><AsRole><VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} /></AsRole></I18nextProvider>);
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.change(screen.getByRole('textbox', { name: `${en.vaults.dialog.name} ${en.vaults.dialog.nameHint}` }), { target: { value: 'AGENT_WALLET' } });
  // A header auth with no header name, which would block an ordinary key.
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.advanced }));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.fetchAuthHeader }));
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  api.createVaultSecret.mockResolvedValue({ ok: true, secret: { accounts: [storedFor(OFFICIAL.split(' '))] } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(screen.queryByText(en.vaults.dialog.errors.authNameRequired)).toBeNull();
});

it('keeps acting on the wallet it created even if the name field changes', async () => {
  await openWallet('WALLET_A');
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  fireEvent.click(screen.getByRole('switch', { name: hd.askAccount0 }));
  let resolveCreate!: (value: unknown) => void;
  api.createVaultSecret.mockReturnValue(new Promise((resolve) => { resolveCreate = resolve; }));
  api.updateVaultHdAccount.mockResolvedValueOnce({ ok: false, code: 'boom' } as never);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  const nameBox = screen.getByRole('textbox', { name: new RegExp(`^${en.vaults.dialog.name}`) }) as HTMLInputElement;
  expect(nameBox.disabled).toBe(true);
  fireEvent.change(nameBox, { target: { value: 'WALLET_B' } });
  await act(async () => { resolveCreate({ ok: true, secret: { accounts: [storedFor(OFFICIAL.split(' '))] } }); });
  await screen.findByText(hd.askFailed);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.askRetry })); });
  expect(api.updateVaultHdAccount.mock.calls.every((call) => call[0] === 'WALLET_A')).toBe(true);
  expect(screen.getByText(hd.done.title.replace('{{name}}', 'WALLET_A'))).toBeTruthy();
});

it('lets an Editor (creator) choose ask-before-signing for #0 at creation', async () => {
  canManage = false;
  try {
    await openWallet('WALLET_E');
    fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
    fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
    const sw = screen.getByRole('switch', { name: hd.askAccount0 }) as HTMLButtonElement;
    expect(sw.disabled).toBe(false);
    fireEvent.click(sw);
    api.createVaultSecret.mockResolvedValue({ ok: true, secret: { accounts: [{ ...storedFor(OFFICIAL.split(' ')), always_ask: true }] } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
    expect(postedPayload().account0_always_ask).toBe(true);
  } finally {
    canManage = true;
  }
});

it('always sends account #0 for the server to check, and shows nothing-saved when it refuses', async () => {
  await openWallet('WALLET_C');
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: hd.import.label }), { target: { value: OFFICIAL } });
  api.createVaultSecret.mockResolvedValue({ ok: false, code: 'hd_address_check_failed', message: 'mismatch' });
  api.deleteVaultSecret.mockClear();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: hd.importCta })); });
  const own = storedFor(OFFICIAL.split(' '));
  expect(api.createVaultSecret).toHaveBeenCalledOnce();
  expect(postedPayload().expected_account0).toEqual({ eth: own.eth, btc_taproot: own.btc_taproot, solana: own.solana });
  expect(await screen.findByText(hd.mismatch)).toBeTruthy();
  expect(screen.getByText(hd.mismatchNotSaved)).toBeTruthy();
  // Nothing was stored: no delete, no local block, no retry without the check.
  expect(api.deleteVaultSecret).not.toHaveBeenCalled();
  expect(localStorage.getItem('avibe.vault.hdAddressCheckFailed') ?? '[]').toBe('[]');
});
