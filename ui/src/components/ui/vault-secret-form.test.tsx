/* @vitest-environment jsdom */

import { webcrypto } from 'node:crypto';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import type { ReactElement } from 'react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import type { VaultSecret } from '@/context/ApiContext';
import { VaultSecretForm } from './vault-secret-form';

const api = vi.hoisted(() => ({
  listDependencies: vi.fn(),
  listVaultSecrets: vi.fn(async () => ({ secrets: [] })),
  listSkills: vi.fn(async () => ({ skills: [] })),
  createVaultSecret: vi.fn(),
  getVaultPubkey: vi.fn(async () => ({ public_key: 'public', fingerprint: 'fingerprint' })),
  deriveSigningAddresses: vi.fn(async () => ({ ok: true, addresses: {} })),
  connectWorkbenchEvents: vi.fn(() => vi.fn()),
}));
const envelope = vi.hoisted(() => ({ ciphertext: 'ciphertext', nonce: 'nonce', wrap_meta: '{}' }));
const blindBox = vi.hoisted(() => ({ scheme: 'test', enc: 'enc', ct: 'ciphertext' }));
const sealBlindBox = vi.hoisted(() => vi.fn(async () => blindBox));
const vault = vi.hoisted(() => ({
  status: 'unlocked', refresh: vi.fn(), afterCreated: vi.fn(),
  sealValue: vi.fn(),
}));
vi.mock('@/context/ApiContext', async (original) => ({
  ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api,
}));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => vault }));
vi.mock('./vault-protected-unlock', () => ({ VaultProtectedUnlock: () => null }));
vi.mock('@/lib/vaultCrypto', async (original) => ({
  ...await original<typeof import('@/lib/vaultCrypto')>(), sealBlindBox,
}));
const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en', fallbackLng: 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false },
});
const form = (element: ReactElement) => <I18nextProvider i18n={i18n}>{element}</I18nextProvider>;
const change = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const applyTemplate = (label: string) => {
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.useTemplate }));
  fireEvent.click(screen.getByRole('menuitem', { name: label }));
};
const nameInputs = () => [1, 2, 3, 4].flatMap((index) => {
  const input = screen.queryByLabelText(`Field ${index} name`) as HTMLInputElement | null;
  return input ? [input.value] : [];
});
const submitButton = () => screen.getByRole('button', { name: 'Save & let the agent continue' }) as HTMLButtonElement;
beforeEach(async () => {
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('isSecureContext', true);
  await i18n.changeLanguage('en');
  api.listDependencies.mockResolvedValue({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.6' }] });
  api.createVaultSecret.mockResolvedValue({ ok: true });
  vault.sealValue.mockImplementation(async (_name, _kind, value) => {
    value.clear();
    return { envelope, establishingVmk: true, authzFactorRegistration: { challenge_id: 'setup' } };
  });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it('blocks protected creation before refresh/seal at an unsupported address, even with stale unlocked state; cancel remains usable', async () => {
  vi.stubGlobal('location', new URL('http://192.0.2.1:5123'));
  vi.stubGlobal('isSecureContext', false);
  const cancel = vi.fn();
  render(form(<VaultSecretForm fixedName="TOKEN" defaultProtection="protected" onCancel={cancel} onCreated={vi.fn()} />));
  fireEvent.change(screen.getByPlaceholderText(en.vaults.dialog.valuePlaceholder), { target: { value: 'public-test-draft' } });
  expect(submitButton().disabled).toBe(true);
  fireEvent.click(submitButton());
  expect(vault.refresh).not.toHaveBeenCalled();
  expect(vault.sealValue).not.toHaveBeenCalled();
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: en.vaults.request.dismiss }));
  expect(cancel).toHaveBeenCalledOnce();
});

// The Generate-key catch owns this presentation independently of the form's submit catch.
// Rejecting the public hook here exercises the actual button and error surface without keys.
it.each(['en', 'zh'] as const)('localizes protected Generate-key integrity/reset errors in %s and preserves unknown-code detail', async (language) => {
  await i18n.changeLanguage(language);
  const copy = language === 'zh' ? zh : en;
  const onCreated = vi.fn();
  render(form(<VaultSecretForm requestSpec={{ kind: 'keypair', protection: 'protected' }} onCancel={vi.fn()} onCreated={onCreated} />));
  fireEvent.change(screen.getByRole('textbox', { name: `${copy.vaults.dialog.name} ${copy.vaults.dialog.nameHint}` }), { target: { value: 'TEST_KEY' } });
  const generate = await screen.findByRole('button', { name: copy.vaults.dialog.signingGenerateCta });
  for (const code of ['sandbox_integrity_failed', 'sandbox_reset', 'future_sandbox_error'] as const) {
    const detail = 'Specific sandbox failure detail';
    vault.sealValue.mockRejectedValueOnce(Object.assign(new Error(detail), { code }));
    await waitFor(() => expect((generate as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(generate);
    const expected = code === 'future_sandbox_error' ? detail : copy.errors[code];
    expect(await screen.findByText(expected)).toBeTruthy();
    if (code !== 'future_sandbox_error') expect(screen.queryByText(detail)).toBeNull();
    expect(vault.sealValue).toHaveBeenLastCalledWith('TEST_KEY', 'keypair');
  }
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  expect(onCreated).not.toHaveBeenCalled();
});

it('checks only avault when the secret form opens, without creating a secret', async () => {
  render(form(<VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} />));
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalledWith({ ids: ['avault'] }));
  expect(api.listDependencies).toHaveBeenCalledTimes(1);
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});

// The form owns row/default selection and wire payload assembly. Existing crypto tests do not
// catch dropping a row, allowing a blank value, or sealing the default under NAME.password.
it.each(['standard', 'protected'] as const)('creates a complete %s login with canonical record names and no plaintext payload', async (protection) => {
  const onCreated = vi.fn();
  render(form(<VaultSecretForm fixedName="GITHUB_LOGIN" defaultProtection={protection} onCancel={vi.fn()} onCreated={onCreated} />));
  applyTemplate('Login + 2FA');
  expect(screen.queryByRole('radio', { name: 'File' })).toBeNull();
  const submit = screen.getByRole('button', { name: 'Save & let the agent continue' }) as HTMLButtonElement;
  change('Value for username', '张三');
  change('Value for password', '密碼');
  expect(submit.disabled).toBe(true);
  change('Value for totp', 'seed');
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith('GITHUB_LOGIN', 'created'));
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload.default_field).toBe('password');
  expect(payload.fields.map((field: { name: string }) => field.name)).toEqual(['username', 'totp']);
  expect(JSON.stringify(payload)).not.toMatch(/张三|密碼|seed/);
  if (protection === 'standard') {
    expect(sealBlindBox.mock.calls.map((call) => (call as unknown[])[2])).toEqual([
      { purpose: 'seal', name: 'GITHUB_LOGIN.username' },
      { purpose: 'seal', name: 'GITHUB_LOGIN' },
      { purpose: 'seal', name: 'GITHUB_LOGIN.totp' },
    ]);
    expect(payload.blind_box).toEqual(blindBox);
    expect(payload.fields).toEqual([
      { name: 'username', record: 'GITHUB_LOGIN.username', blind_box: blindBox },
      { name: 'totp', record: 'GITHUB_LOGIN.totp', blind_box: blindBox },
    ]);
  } else {
    expect(vault.sealValue.mock.calls.map((call) => call[0])).toEqual(['GITHUB_LOGIN.username', 'GITHUB_LOGIN', 'GITHUB_LOGIN.totp']);
    expect(payload.sealed).toEqual(envelope);
    expect(payload.establishing_vmk).toBe(true);
    expect(payload.authz_factor_registration).toEqual({ challenge_id: 'setup' });
    expect(payload.fields).toEqual([
      { name: 'username', record: 'GITHUB_LOGIN.username', sealed: envelope },
      { name: 'totp', record: 'GITHUB_LOGIN.totp', sealed: envelope },
    ]);
    expect(vault.afterCreated).toHaveBeenCalledOnce();
  }
});

it('keeps the legacy single-value create payload until fields are added', async () => {
  render(form(<VaultSecretForm fixedName="TOKEN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  fireEvent.change(screen.getByPlaceholderText(en.vaults.dialog.valuePlaceholder), { target: { value: 'token' } });
  const submit = screen.getByRole('button', { name: 'Save & let the agent continue' }) as HTMLButtonElement;
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(api.createVaultSecret.mock.calls[0][0]).not.toHaveProperty('fields');
  expect(api.createVaultSecret.mock.calls[0][0]).not.toHaveProperty('default_field');
});

it('preserves a selected file instead of silently switching it to empty field rows', async () => {
  const { container } = render(form(<VaultSecretForm fixedName="TOKEN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  fireEvent.click(screen.getByRole('radio', { name: 'File' }));
  const file = new File(['secret bytes'], 'credential.bin');
  fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
  expect(screen.getByText('credential.bin')).toBeTruthy();
  const add = screen.getByRole('button', { name: 'Add field' }) as HTMLButtonElement;
  const template = screen.getByRole('button', { name: en.vaults.fields.useTemplate }) as HTMLButtonElement;
  expect(add.disabled).toBe(true);
  expect(template.disabled).toBe(true);
  fireEvent.click(add);
  fireEvent.click(template);
  expect(screen.getByText('credential.bin')).toBeTruthy();
  expect(screen.getByText(en.vaults.fields.singleFile)).toBeTruthy();
  expect(screen.queryByLabelText('Field 1 name')).toBeNull();
});

it('seeds provision rows once, validates duplicate names, and permits removing extra rows', async () => {
  const spec = { fields: ['username', 'password', 'totp'], default_field: 'password' };
  const props = { fixedName: 'LOGIN', provisionRequestId: 'request', onCancel: vi.fn(), onCreated: vi.fn() };
  const { rerender } = render(form(<VaultSecretForm {...props} requestSpec={spec} />));
  change('Value for username', '用户');
  change('Value for password', 'secret');
  fireEvent.click(screen.getByRole('button', { name: 'Remove totp' }));
  rerender(form(<VaultSecretForm {...props} requestSpec={{ ...spec }} />));
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe('用户');
  expect(screen.queryByLabelText('Value for totp')).toBeNull();
  change('Field 1 name', 'password');
  const submit = screen.getByRole('button', { name: 'Save & let the agent continue' }) as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  change('Field 1 name', 'user_name');
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(api.createVaultSecret.mock.calls[0][0].fields).toEqual([
    { name: 'user_name', record: 'LOGIN.user_name', blind_box: blindBox },
  ]);
});

it('does not overwrite edited metadata when a refreshed secret object arrives', () => {
  const first: VaultSecret = {
    name: 'LOGIN',
    default_field: 'password',
    fields: ['password', 'username'],
    kind: 'static',
    protection: 'standard',
    signer_kind: null,
    source: 'user',
    tags: [],
    policy: {},
    description: 'server description',
    last_used_at: null,
    use_count: 0,
    created_at: 'created',
    updated_at: 'version-1',
  };
  const refreshed = { ...first, description: 'new server description', updated_at: 'version-2' };
  const props = { onCancel: vi.fn(), onCreated: vi.fn(), editSecret: first };
  const { rerender } = render(form(<VaultSecretForm {...props} />));
  const description = screen.getByLabelText('Description') as HTMLInputElement;
  fireEvent.change(description, { target: { value: 'local unsaved description' } });
  rerender(form(<VaultSecretForm {...props} editSecret={refreshed} />));
  expect((screen.getByLabelText('Description') as HTMLInputElement).value).toBe('local unsaved description');
});

// A template decides the field shape and where an already-typed single value lands. The payload
// test above starts from an empty value, so it cannot catch the value moving to the wrong row.
it.each([
  ['Login', ['username', 'password']],
  ['Login + 2FA', ['username', 'password', 'totp']],
] as const)('applies the %s template with the typed value in its default password field', (label, fields) => {
  render(form(<VaultSecretForm fixedName="LOGIN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  fireEvent.change(screen.getByPlaceholderText(en.vaults.dialog.valuePlaceholder), { target: { value: 'typed-secret' } });
  applyTemplate(label);
  expect(nameInputs()).toEqual(fields);
  expect(fields.map((field) => (screen.getByLabelText(`Value for ${field}`) as HTMLInputElement).value))
    .toEqual(fields.map((field) => (field === 'password' ? 'typed-secret' : '')));
  expect(screen.queryByRole('button', { name: 'Set password as default' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Set username as default' })).toBeTruthy();
});

// Set default decides which field owns the bare secret record, and the default row stays put.
it('moves the bare record to the field set as default and keeps the default row', async () => {
  render(form(<VaultSecretForm fixedName="LOGIN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  applyTemplate('Login');
  change('Value for username', 'alice');
  change('Value for password', 'secret');
  fireEvent.click(screen.getByRole('button', { name: 'Set username as default' }));
  const removeDefault = screen.getByRole('button', { name: 'Remove username' });
  expect(removeDefault.getAttribute('aria-disabled')).toBe('true');
  expect(removeDefault.getAttribute('title')).toBe(en.vaults.fields.defaultNotRemovable);
  fireEvent.click(removeDefault);
  fireEvent.click(screen.getByRole('button', { name: 'Actions for username' }));
  expect((screen.getByRole('menuitem', { name: new RegExp(en.vaults.fields.removeField) }) as HTMLButtonElement).disabled).toBe(true);
  expect(nameInputs()).toEqual(['username', 'password']);
  await waitFor(() => expect(submitButton().disabled).toBe(false));
  fireEvent.click(submitButton());
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload.default_field).toBe('username');
  expect(payload.fields).toEqual([{ name: 'password', record: 'LOGIN.password', blind_box: blindBox }]);
});

// A row holding a value asks inside the row, from either the desktop × or the phone menu.
it('confirms removing a filled row inline', () => {
  render(form(<VaultSecretForm fixedName="LOGIN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  applyTemplate('Login + 2FA');
  change('Value for totp', 'seed');
  fireEvent.click(screen.getByRole('button', { name: 'Remove totp' }));
  expect(screen.getByText(/and its value\?/).textContent).toBe('Remove totp and its value?');
  expect(screen.queryByRole('dialog')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.cancel }));
  expect((screen.getByLabelText('Value for totp') as HTMLInputElement).value).toBe('seed');
  fireEvent.click(screen.getByRole('button', { name: 'Actions for totp' }));
  fireEvent.click(screen.getByRole('menuitem', { name: en.vaults.fields.removeField }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect(screen.queryByLabelText('Value for totp')).toBeNull();
  expect(nameInputs()).toEqual(['username', 'password']);
});

// Each rule is reported under the row that breaks it; a blank row waits until it was visited.
it('reports field problems on the offending row', () => {
  render(form(<VaultSecretForm fixedName="LOGIN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  applyTemplate('Login');
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  const added = screen.getByLabelText('Field 3 name');
  fireEvent.focus(added);
  expect(screen.getByText(en.vaults.fields.nameRule)).toBeTruthy();
  expect(screen.queryByText(en.vaults.fields.nameRequired)).toBeNull();
  fireEvent.blur(added);
  expect(screen.queryByText(en.vaults.fields.nameRule)).toBeNull();
  expect(screen.getByText(en.vaults.fields.nameRequired).id).toBe(added.getAttribute('aria-describedby'));
  change('Field 3 name', 'TOTP');
  expect(screen.getByText(en.vaults.fields.nameInvalid)).toBeTruthy();
  change('Field 3 name', 'username');
  expect(screen.getAllByText("There's already a username field")).toHaveLength(2);
  change('Field 3 name', 'totp');
  expect(screen.queryByText("There's already a username field")).toBeNull();
  const totpValue = screen.getByLabelText('Value for totp');
  fireEvent.blur(totpValue);
  expect(document.getElementById(totpValue.getAttribute('aria-describedby')!)!.textContent).toBe(en.vaults.fields.valueRequired);
  // A value problem marks the value input, not the valid name beside it.
  expect(totpValue.getAttribute('aria-invalid')).toBe('true');
  expect(added.hasAttribute('aria-invalid')).toBe(false);
  expect(added.hasAttribute('aria-describedby')).toBe(false);
  expect(submitButton().disabled).toBe(true);
});

// The preview lists exactly what an agent can reference: valid fields, and the bare name for the default.
it('previews agent references for valid fields and the default shorthand', () => {
  render(form(<VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} />));
  applyTemplate('Login + 2FA');
  const preview = () => screen.getByText(en.vaults.fields.references).parentElement!.parentElement!;
  expect(preview().textContent).toBe(
    'Agent referencesNAME.usernameNAME.passwordNAME.totpNAME alone resolves to the default field, password.',
  );
  fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'GITHUB_LOGIN' } });
  change('Field 3 name', 'TOTP');
  expect(preview().textContent).toBe(
    'Agent referencesGITHUB_LOGIN.usernameGITHUB_LOGIN.passwordGITHUB_LOGIN alone resolves to the default field, password.',
  );
});

// ---- Solana signing keys (ui/design: vault/solana/*) ----
// avault creates these keys, so the form's contract is what it sends and what it refuses to hold.
const startSolanaKey = async (name = 'SOL_KEY') => {
  render(form(<VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} />));
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.change(screen.getByRole('textbox', { name: `${en.vaults.dialog.name} ${en.vaults.dialog.nameHint}` }), { target: { value: name } });
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.chainSolana }));
};
const createButton = () => screen.getByRole('button', { name: en.vaults.dialog.createSecret }) as HTMLButtonElement;
const tier = (title: string) => screen.getByRole('button', { name: new RegExp(`^${title}`) });

it('asks avault to generate a Solana key and sends no key material or public key', async () => {
  await startSolanaKey();
  await waitFor(() => expect(createButton().disabled).toBe(false));
  fireEvent.click(createButton());
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload).toMatchObject({
    name: 'SOL_KEY', kind: 'keypair', signer_kind: 'local', protection: 'standard',
    keypair: { curve: 'ed25519', source: 'generate' },
  });
  for (const field of ['blind_box', 'sealed', 'public_meta']) expect(payload).not.toHaveProperty(field);
  expect(sealBlindBox).not.toHaveBeenCalled();
});

it.each([
  ['[1, 2, 3]', 'solana-cli-json'],
  ['  notARealBase58Secret  ', 'base58-secret'],
] as const)('seals a pasted Solana key %s for avault as %s and posts no plaintext', async (pasted, format) => {
  await startSolanaKey();
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.signingImport }));
  expect(createButton().disabled).toBe(true);
  fireEvent.change(screen.getByPlaceholderText(en.vaults.dialog.solanaImportPlaceholder), { target: { value: pasted } });
  await waitFor(() => expect(createButton().disabled).toBe(false));
  fireEvent.click(createButton());
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(sealBlindBox).toHaveBeenCalledExactlyOnceWith(
    pasted.trim(),
    { public_key: 'public', fingerprint: 'fingerprint' },
    { purpose: 'keypair-import', name: 'SOL_KEY', curve: 'ed25519', format },
  );
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload.keypair).toEqual({ curve: 'ed25519', source: 'import', format });
  expect(payload.blind_box).toEqual(blindBox);
  expect(payload).not.toHaveProperty('public_meta');
  expect(JSON.stringify(payload)).not.toContain(pasted.trim());
});

it('says what to check when avault does not import the pasted key, and keeps it to correct', async () => {
  const onCreated = vi.fn();
  api.createVaultSecret.mockResolvedValue({ ok: false, code: 'avault_failed', message: 'avault keypair failed: unreadable detail' });
  render(form(<VaultSecretForm onCancel={vi.fn()} onCreated={onCreated} />));
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.change(screen.getByRole('textbox', { name: `${en.vaults.dialog.name} ${en.vaults.dialog.nameHint}` }), { target: { value: 'SOL_KEY' } });
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.chainSolana }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.signingImport }));
  const paste = screen.getByPlaceholderText(en.vaults.dialog.solanaImportPlaceholder) as HTMLTextAreaElement;
  fireEvent.change(paste, { target: { value: '[1, 2, 3]' } });
  await waitFor(() => expect(createButton().disabled).toBe(false));
  fireEvent.click(createButton());
  expect(await screen.findByText(en.vaults.dialog.errors.solanaImportFailed)).toBeTruthy();
  expect(screen.queryByText(/unreadable detail/)).toBeNull();
  expect(paste.value).toBe('[1, 2, 3]');
  expect(onCreated).not.toHaveBeenCalled();
  // Editing the key withdraws the message: it described the previous attempt.
  fireEvent.change(paste, { target: { value: '[1, 2, 3, 4]' } });
  expect(screen.queryByText(en.vaults.dialog.errors.solanaImportFailed)).toBeNull();
});

it('drops what was made or pasted for one chain when the chain changes', async () => {
  render(form(<VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} />));
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.change(screen.getByRole('textbox', { name: `${en.vaults.dialog.name} ${en.vaults.dialog.nameHint}` }), { target: { value: 'KEY' } });
  // A secp256k1 key generated in this browser must not survive a switch to Solana and back.
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.signingGenerateCta }));
  await waitFor(() => expect(createButton().disabled).toBe(false));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.chainSolana }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByPlaceholderText(en.vaults.dialog.solanaImportPlaceholder), { target: { value: '[1, 2, 3]' } });
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.chainEvmBitcoin }));
  expect(createButton().disabled).toBe(true);
  expect(screen.queryByRole('button', { name: en.vaults.dialog.signingRegenerate })).toBeNull();
  // And a pasted Solana key must not survive the round trip either.
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.chainSolana }));
  expect((screen.getByPlaceholderText(en.vaults.dialog.solanaImportPlaceholder) as HTMLTextAreaElement).value).toBe('');
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});

it('keeps a chosen Protected tier for a Solana key, blocks Create, and never opens the passkey vault for it', async () => {
  await startSolanaKey();
  fireEvent.click(tier(en.vaults.dialog.protectedProtection));
  // The tier is the person's choice: it is not switched back for them.
  expect(tier(en.vaults.dialog.protectedProtection).getAttribute('aria-pressed')).toBe('true');
  expect(screen.getByRole('alert').textContent).toBe(en.vaults.dialog.solanaProtectedUnavailable);
  expect(screen.queryByRole('button', { name: en.vaults.dialog.signingImport })).toBeNull();
  expect(createButton().disabled).toBe(true);
  fireEvent.click(createButton());
  expect(vault.refresh).not.toHaveBeenCalled();
  expect(vault.sealValue).not.toHaveBeenCalled();
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  fireEvent.click(tier(en.vaults.dialog.standardProtection));
  expect(screen.queryByText(en.vaults.dialog.solanaProtectedUnavailable)).toBeNull();
  await waitFor(() => expect(createButton().disabled).toBe(false));
});

it('shows the chain of a Solana key in Edit, where it cannot be changed', () => {
  const key: VaultSecret = {
    name: 'SOL_KEY', tags: [], kind: 'keypair', protection: 'standard', signer_kind: 'local', signing_curve: 'ed25519',
    source: 'user', policy: {}, last_used_at: null, use_count: 0, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  };
  const { rerender } = render(form(<VaultSecretForm editSecret={key} onCancel={vi.fn()} onCreated={vi.fn()} />));
  expect(screen.getByText(en.vaults.dialog.chainSolana)).toBeTruthy();
  expect(screen.queryByRole('radio', { name: en.vaults.dialog.chainSolana })).toBeNull();
  rerender(form(<VaultSecretForm editSecret={{ ...key, name: 'ETH_KEY', signing_curve: 'secp256k1' }} onCancel={vi.fn()} onCreated={vi.fn()} />));
  expect(screen.queryByText(en.vaults.dialog.chainSolana)).toBeNull();
});
