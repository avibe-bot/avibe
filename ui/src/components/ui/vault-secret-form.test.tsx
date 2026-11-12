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
    if (typeof value === 'object' && value !== null && 'clear' in value && typeof value.clear === 'function') value.clear();
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

// A protected key is generated in the sandbox when the secret is created, so the submit catch owns
// this presentation. Rejecting the public hook exercises the real button and error surface without keys.
it.each(['en', 'zh'] as const)('localizes protected key-generation integrity/reset errors in %s and preserves unknown-code detail', async (language) => {
  await i18n.changeLanguage(language);
  const copy = language === 'zh' ? zh : en;
  const onCreated = vi.fn();
  render(form(<VaultSecretForm requestSpec={{ kind: 'keypair', protection: 'protected' }} onCancel={vi.fn()} onCreated={onCreated} />));
  fireEvent.change(screen.getByRole('textbox', { name: `${copy.vaults.dialog.name} ${copy.vaults.dialog.nameHint}` }), { target: { value: 'TEST_KEY' } });
  // No Generate button: the key is made on create, so no address can be shown and then replaced.
  expect(screen.queryByRole('button', { name: /generate|生成/i })).toBeNull();
  const create = screen.getByRole('button', { name: copy.vaults.dialog.createSecret }) as HTMLButtonElement;
  for (const code of ['sandbox_integrity_failed', 'sandbox_reset', 'future_sandbox_error'] as const) {
    const detail = 'Specific sandbox failure detail';
    vault.sealValue.mockRejectedValueOnce(Object.assign(new Error(detail), { code }));
    await waitFor(() => expect(create.disabled).toBe(false));
    fireEvent.click(create);
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

it('generates a standard static value only after an explicit click and persists only its blind box', async () => {
  render(form(<VaultSecretForm fixedName="GENERATED_TOKEN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.generateStatic }));
  const input = await screen.findByPlaceholderText(en.vaults.dialog.valuePlaceholder) as HTMLInputElement;
  await waitFor(() => expect(input.value).toMatch(/^[A-Za-z0-9_-]{32}$/));
  const generated = input.value;
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  fireEvent.click(submitButton());
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(sealBlindBox).toHaveBeenCalledWith(generated, expect.anything(), { purpose: 'seal', name: 'GENERATED_TOKEN' });
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload.blind_box).toEqual(blindBox);
  expect(JSON.stringify(payload)).not.toContain(generated);
});

it('generates and saves a standard request recipe from one click', async () => {
  render(form(<VaultSecretForm
    fixedName="REQUEST_TOKEN"
    provisionRequestId="request-standard-generated"
    requestSpec={{ generate: { length: 32, charset: 'base64url' } }}
    onCancel={vi.fn()}
    onCreated={vi.fn()}
  />));
  const submit = screen.getByRole('button', { name: en.vaults.request.generateAndWake }) as HTMLButtonElement;
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);

  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(sealBlindBox).toHaveBeenCalledOnce();
  const generated = sealBlindBox.mock.calls[0][0] as string;
  expect(generated).toMatch(/^[A-Za-z0-9_-]{32}$/);
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload.provision_request_id).toBe('request-standard-generated');
  expect(payload.blind_box).toEqual(blindBox);
  expect(payload).not.toHaveProperty('generate');
  expect(JSON.stringify(payload)).not.toContain(generated);
});

it.each([
  ['en', en.vaults.request.generateAndWake],
  ['zh', zh.vaults.request.generateAndWake],
] as const)('localizes the one-click request action in %s', async (language, label) => {
  await i18n.changeLanguage(language);
  render(form(<VaultSecretForm
    fixedName="REQUEST_TOKEN"
    provisionRequestId={`request-${language}`}
    requestSpec={{ generate: { length: 32, charset: 'hex' } }}
    onCancel={vi.fn()}
    onCreated={vi.fn()}
  />));
  expect(await screen.findByRole('button', { name: label })).toBeTruthy();
});

it('generates only missing standard request fields and preserves manually entered values', async () => {
  const recipe = { length: 48, charset: 'hex' } as const;
  render(form(<VaultSecretForm
    fixedName="LOGIN"
    provisionRequestId="request-standard-fields"
    requestSpec={{
      protection: 'standard',
      fields: ['username', 'password'],
      default_field: 'password',
      generate: recipe,
    }}
    onCancel={vi.fn()}
    onCreated={vi.fn()}
  />));
  await waitFor(() => expect(screen.getByLabelText('Value for username')).toBeTruthy());
  change('Value for username', 'manual-user');
  const submit = screen.getByRole('button', { name: en.vaults.request.generateAndWake }) as HTMLButtonElement;
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);

  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(sealBlindBox).toHaveBeenCalledTimes(2);
  expect(sealBlindBox).toHaveBeenNthCalledWith(
    1,
    'manual-user',
    { public_key: 'public', fingerprint: 'fingerprint' },
    { purpose: 'seal', name: 'LOGIN.username' },
  );
  expect(sealBlindBox).toHaveBeenNthCalledWith(
    2,
    expect.stringMatching(/^[0-9a-f]{48}$/),
    { public_key: 'public', fingerprint: 'fingerprint' },
    { purpose: 'seal', name: 'LOGIN' },
  );

  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload).toMatchObject({
    provision_request_id: 'request-standard-fields',
    default_field: 'password',
    blind_box: blindBox,
    fields: [{ name: 'username', record: 'LOGIN.username', blind_box: blindBox }],
  });
  expect(payload).not.toHaveProperty('generate');
  expect(JSON.stringify(payload)).not.toContain('manual-user');
});

it('uses a protected request recipe only on user action and submits the sandbox envelope', async () => {
  render(form(<VaultSecretForm
    fixedName="DEPLOYMENT_ROOT_KEY"
    provisionRequestId="request-generated"
    requestSpec={{ protection: 'protected', generate: { length: 64, charset: 'hex' } }}
    onCancel={vi.fn()}
    onCreated={vi.fn()}
  />));
  await waitFor(() => expect((screen.getByLabelText(en.vaults.dialog.generationLength) as HTMLInputElement).value).toBe('64'));
  expect(vault.sealValue).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.generateStatic }));
  await screen.findByText(en.vaults.dialog.generatedProtected);
  expect(vault.sealValue).toHaveBeenCalledWith('DEPLOYMENT_ROOT_KEY', 'static', { generate: { length: 64, charset: 'hex' } });
  expect(api.createVaultSecret).not.toHaveBeenCalled();
  fireEvent.click(submitButton());
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload.sealed).toEqual(envelope);
  expect(payload).not.toHaveProperty('generate');
  expect(payload).not.toHaveProperty('value');
  expect(JSON.stringify(payload)).not.toContain('plaintext');
});

it('generates only missing protected request fields and preserves manually entered values', async () => {
  const recipe = { length: 48, charset: 'hex' } as const;
  render(form(<VaultSecretForm
    fixedName="LOGIN"
    provisionRequestId="request-protected-fields"
    requestSpec={{
      protection: 'protected',
      fields: ['username', 'password'],
      default_field: 'password',
      generate: recipe,
    }}
    onCancel={vi.fn()}
    onCreated={vi.fn()}
  />));
  await waitFor(() => expect(screen.getByLabelText('Value for username')).toBeTruthy());
  change('Value for username', 'manual-user');
  const submit = screen.getByRole('button', { name: en.vaults.request.generateAndWake }) as HTMLButtonElement;
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);

  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(vault.sealValue).toHaveBeenCalledTimes(2);
  expect(vault.sealValue).toHaveBeenCalledWith('LOGIN', 'static', { generate: recipe });
  expect(vault.sealValue).toHaveBeenCalledWith('LOGIN.username', 'static', expect.objectContaining({
    valueRef: expect.objectContaining({ current: 'manual-user' }),
    clear: expect.any(Function),
  }));
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload).toMatchObject({
    provision_request_id: 'request-protected-fields',
    default_field: 'password',
    sealed: envelope,
    fields: [{ name: 'username', record: 'LOGIN.username', sealed: envelope }],
  });
  expect(payload).not.toHaveProperty('generate');
  expect(JSON.stringify(payload)).not.toContain('manual-user');
});

it('does not save after protected request generation fails and lets the user retry', async () => {
  vault.sealValue.mockRejectedValueOnce(Object.assign(new Error('sandbox temporarily unavailable'), { code: 'sandbox_reset' }));
  render(form(<VaultSecretForm
    fixedName="REQUEST_TOKEN"
    provisionRequestId="request-protected-retry"
    requestSpec={{ protection: 'protected', generate: { length: 32, charset: 'hex' } }}
    onCancel={vi.fn()}
    onCreated={vi.fn()}
  />));
  const submit = screen.getByRole('button', { name: en.vaults.request.generateAndWake }) as HTMLButtonElement;
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);
  expect(await screen.findByText(en.errors.sandbox_reset)).toBeTruthy();
  expect(api.createVaultSecret).not.toHaveBeenCalled();

  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(api.createVaultSecret.mock.calls[0][0].sealed).toEqual(envelope);
});

it('seals each protected generated field in the sandbox and reuses its field-bound envelope', async () => {
  render(form(<VaultSecretForm fixedName="LOGIN" defaultProtection="protected" onCancel={vi.fn()} onCreated={vi.fn()} />));
  applyTemplate('Login');
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.generateNamed.replace('{{field}}', 'username') }));
  await screen.findByText(en.vaults.dialog.generatedProtected);
  change('Value for password', 'manual-password');
  fireEvent.click(submitButton());
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(vault.sealValue).toHaveBeenCalledWith('LOGIN.username', 'static', { generate: { length: 32, charset: 'base64url' } });
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload.sealed).toEqual(envelope);
  expect(payload.default_field).toBe('password');
  expect(payload.fields).toEqual([{ name: 'username', record: 'LOGIN.username', sealed: envelope }]);
  expect(JSON.stringify(payload)).not.toContain('manual-password');
});

it('moves a sandbox-generated single value onto the default row when creating fields', async () => {
  render(form(<VaultSecretForm fixedName="LOGIN" defaultProtection="protected" onCancel={vi.fn()} onCreated={vi.fn()} />));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.generateStatic }));
  await screen.findByText(en.vaults.dialog.generatedProtected);
  applyTemplate('Login');
  change('Value for username', 'alice');
  fireEvent.click(submitButton());

  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(vault.sealValue).toHaveBeenCalledWith('LOGIN', 'static', { generate: { length: 32, charset: 'base64url' } });
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload.sealed).toEqual(envelope);
  expect(payload.default_field).toBe('password');
  expect(payload.fields).toEqual([{ name: 'username', record: 'LOGIN.username', sealed: envelope }]);
});

it('discards a protected generation resolved after the record name changes', async () => {
  let resolveSeal: (result: { envelope: typeof envelope; establishingVmk: boolean }) => void = () => {};
  vault.sealValue.mockImplementationOnce(() => new Promise((resolve) => { resolveSeal = resolve; }));
  const { container } = render(form(<VaultSecretForm defaultProtection="protected" onCancel={vi.fn()} onCreated={vi.fn()} />));
  const name = screen.getByRole('textbox', { name: `${en.vaults.dialog.name} ${en.vaults.dialog.nameHint}` });
  fireEvent.change(name, { target: { value: 'LOGIN' } });
  fireEvent.click(screen.getByRole('button', { name: en.vaults.dialog.generateStatic }));
  await waitFor(() => expect(vault.sealValue).toHaveBeenCalledOnce());
  fireEvent.change(name, { target: { value: 'RENAMED' } });
  fireEvent.change(name, { target: { value: 'LOGIN' } });
  resolveSeal({ envelope, establishingVmk: false });

  await waitFor(() => expect((screen.getByRole('button', { name: en.vaults.dialog.generateStatic }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByText(en.vaults.dialog.generatedProtected)).toBeNull();
  expect((container.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(true);
});

it.each(['standard', 'protected'] as const)('invalidates generated %s field values when the recipe changes', async (protection) => {
  render(form(<VaultSecretForm fixedName="LOGIN" defaultProtection={protection} onCancel={vi.fn()} onCreated={vi.fn()} />));
  applyTemplate('Login');
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.generateNamed.replace('{{field}}', 'username') }));

  if (protection === 'protected') {
    await screen.findByText(en.vaults.dialog.generatedProtected);
  } else {
    await waitFor(() => expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toMatch(/^[A-Za-z0-9_-]{32}$/));
  }

  fireEvent.change(screen.getByLabelText(en.vaults.dialog.generationLength), { target: { value: '48' } });

  if (protection === 'protected') {
    await waitFor(() => expect(screen.queryByText(en.vaults.dialog.generatedProtected)).toBeNull());
  } else {
    await waitFor(() => expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe(''));
  }
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
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
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
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
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
  // A secp256k1 key pasted in this browser must not survive a switch to Solana and back.
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByRole('textbox', { name: en.vaults.dialog.signingImport }), { target: { value: '11'.repeat(32) } });
  await waitFor(() => expect(createButton().disabled).toBe(false));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.chainSolana }));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  fireEvent.change(screen.getByPlaceholderText(en.vaults.dialog.solanaImportPlaceholder), { target: { value: '[1, 2, 3]' } });
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.chainEvmBitcoin }));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.signingImport }));
  expect((screen.getByRole('textbox', { name: en.vaults.dialog.signingImport }) as HTMLInputElement).value).toBe('');
  expect(createButton().disabled).toBe(true);
  // And a pasted Solana key must not survive the round trip either.
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.chainSolana }));
  expect((screen.getByPlaceholderText(en.vaults.dialog.solanaImportPlaceholder) as HTMLTextAreaElement).value).toBe('');
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});

// A protected Solana key is born in the sandbox as an Ed25519 record. The form must ask for that
// curve, pin the key exactly as the record names it, and send nothing avault would create a key from.
const solanaRecord = {
  envelope: { ciphertext: 'ciphertext', nonce: 'nonce', wrap_meta: '{"record_meta":{"kind":"keypair","curve":"ed25519"}}' },
  establishingVmk: false,
  publicKey: 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a',
  addresses: { solana: 'FVen3X669xLzsi6N2V91DoiyzHzg1uAgqiT8jZ9nS96Z' },
};

it('creates a protected Solana key from an Ed25519 record the sandbox generated', async () => {
  vault.sealValue.mockResolvedValueOnce(solanaRecord);
  await startSolanaKey();
  fireEvent.click(tier(en.vaults.dialog.protectedProtection));
  expect(screen.queryByRole('radio', { name: en.vaults.dialog.signingImport })).toBeNull();
  // Nothing is generated, and no address shown, before the secret is created.
  expect(await screen.findByText(en.vaults.dialog.signingCreateHint)).toBeTruthy();
  expect(vault.sealValue).not.toHaveBeenCalled();
  await waitFor(() => expect(createButton().disabled).toBe(false));
  fireEvent.click(createButton());
  await waitFor(() => expect(vault.sealValue).toHaveBeenCalledExactlyOnceWith('SOL_KEY', 'keypair', undefined, 'ed25519'));
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  const payload = api.createVaultSecret.mock.calls[0][0];
  expect(payload).toMatchObject({ name: 'SOL_KEY', protection: 'protected', kind: 'keypair', signer_kind: 'local', sealed: solanaRecord.envelope });
  expect(payload.public_meta).toEqual({
    signing_public_key: { curve: 'ed25519', chain: 'solana', public_key: solanaRecord.publicKey },
  });
  for (const field of ['keypair', 'blind_box']) expect(payload).not.toHaveProperty(field);
  expect(sealBlindBox).not.toHaveBeenCalled();
});

it('saves nothing when the sandbox cannot create a Solana key', async () => {
  vault.sealValue.mockRejectedValueOnce(Object.assign(new Error('The sandbox did not create an Ed25519 keypair record'), { code: 'sandbox_curve_unsupported' }));
  await startSolanaKey();
  fireEvent.click(tier(en.vaults.dialog.protectedProtection));
  await waitFor(() => expect(createButton().disabled).toBe(false));
  fireEvent.click(createButton());
  expect(await screen.findByText(en.errors.sandbox_curve_unsupported)).toBeTruthy();
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});

it('still asks the sandbox for a secp256k1 key when EVM / Bitcoin is chosen', async () => {
  vault.sealValue.mockResolvedValueOnce({ ...solanaRecord, publicKey: `02${'11'.repeat(32)}`, addresses: {} });
  render(form(<VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} />));
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.change(screen.getByRole('textbox', { name: `${en.vaults.dialog.name} ${en.vaults.dialog.nameHint}` }), { target: { value: 'ETH_KEY' } });
  fireEvent.click(tier(en.vaults.dialog.protectedProtection));
  await waitFor(() => expect(createButton().disabled).toBe(false));
  fireEvent.click(createButton());
  await waitFor(() => expect(vault.sealValue).toHaveBeenCalledExactlyOnceWith('ETH_KEY', 'keypair'));
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  expect(api.createVaultSecret.mock.calls[0][0].public_meta.signing_public_key).toEqual({ curve: 'secp256k1', public_key: `02${'11'.repeat(32)}` });
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
