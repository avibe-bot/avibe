/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import type { ReactElement } from 'react';
import en from '@/i18n/en.json';
import type { VaultSecret } from '@/context/ApiContext';
import { VaultSecretForm } from './vault-secret-form';

const api = vi.hoisted(() => ({
  listDependencies: vi.fn(),
  listVaultSecrets: vi.fn(async () => ({ secrets: [] })),
  listSkills: vi.fn(async () => ({ skills: [] })),
  createVaultSecret: vi.fn(),
  getVaultPubkey: vi.fn(async () => ({ public_key: 'public', fingerprint: 'fingerprint' })),
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
  lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false },
});
const form = (element: ReactElement) => <I18nextProvider i18n={i18n}>{element}</I18nextProvider>;
const change = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
beforeEach(() => {
  api.listDependencies.mockResolvedValue({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.6' }] });
  api.createVaultSecret.mockResolvedValue({ ok: true });
  vault.sealValue.mockImplementation(async (_name, _kind, value) => {
    value.clear();
    return { envelope, establishingVmk: true, authzFactorRegistration: { challenge_id: 'setup' } };
  });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

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
  fireEvent.click(screen.getByRole('button', { name: 'Login template' }));
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
  const template = screen.getByRole('button', { name: 'Login template' }) as HTMLButtonElement;
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
