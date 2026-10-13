// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ApiError, type VaultSecret } from '@/context/ApiContext';
import en from '@/i18n/en.json';
import { VaultSecretFieldsEditor } from './vault-secret-fields-editor';

const api = vi.hoisted(() => ({
  updateVaultSecret: vi.fn(), updateVaultSecretField: vi.fn(), deleteVaultSecretField: vi.fn(),
  listVaultSecrets: vi.fn(),
  getVaultPubkey: vi.fn(async () => ({ public_key: 'public' })),
}));
const box = vi.hoisted(() => ({ scheme: 'test', enc: 'enc', ct: 'ciphertext' }));
const sealBlindBox = vi.hoisted(() => vi.fn(async () => box));
const vault = vi.hoisted(() => ({ status: 'unlocked', refresh: vi.fn(), sealValue: vi.fn() }));
vi.mock('@/context/ApiContext', async (original) => ({
  ...await original<typeof import('@/context/ApiContext')>(),
  useApi: () => api,
}));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => vault }));
vi.mock('./vault-protected-unlock', () => ({ VaultProtectedUnlock: () => null }));
vi.mock('@/lib/vaultCrypto', async (original) => ({
  ...await original<typeof import('@/lib/vaultCrypto')>(), sealBlindBox,
}));
const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false },
});
const secret: VaultSecret = {
  name: 'LOGIN', default_field: 'password', fields: ['password', 'username'],
  kind: 'static', protection: 'standard', signer_kind: null, source: 'user', tags: [],
  policy: {}, last_used_at: null, use_count: 0, created_at: 'created', updated_at: 'version-1',
};
const change = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const renderEditor = (entry = secret) => render(
  <I18nextProvider i18n={i18n}><VaultSecretFieldsEditor secret={entry} disabled={false} onBusyChange={vi.fn()} /></I18nextProvider>,
);
beforeEach(() => {
  api.updateVaultSecretField.mockResolvedValue({ ok: true, secret: { ...secret, updated_at: 'version-2' } });
  api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: [secret] });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

// The editor owns metadata versions between browser encryptions. Form creation and crypto
// tests cannot catch a second field write reusing the first version, or a default rename
// accidentally turning its replacement into a dotted record.
it('updates envelope versions after replacing a field and renaming the default label', async () => {
  renderEditor();
  change('Value for password', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledWith(
    'LOGIN', 'password', { record: 'LOGIN', blind_box: box, expected_updated_at: 'version-1' }, { handleError: false },
  ));
  await screen.findByRole('status');
  expect((screen.getByLabelText('Value for password') as HTMLInputElement).value).toBe('');
  change('Value for username', 'next-user');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenLastCalledWith(
    'LOGIN', 'username', { record: 'LOGIN.username', blind_box: box, expected_updated_at: 'version-2' }, { handleError: false },
  ));
  await waitFor(() => expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe(''));
  api.updateVaultSecret.mockResolvedValue({ ok: true, secret: { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-3' } });
  change('Default field name', 'credential');
  fireEvent.click(screen.getByRole('button', { name: 'Rename' }));
  await screen.findByLabelText('Value for credential');
  expect(api.updateVaultSecret).toHaveBeenCalledWith('LOGIN', { default_field: 'credential' }, { handleError: false });
  api.updateVaultSecretField.mockResolvedValue({ ok: true, secret: { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-4' } });
  change('Value for credential', 'next');
  fireEvent.click(screen.getByRole('button', { name: 'Save credential' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenLastCalledWith(
    'LOGIN', 'credential', { record: 'LOGIN', blind_box: box, expected_updated_at: 'version-3' }, { handleError: false },
  ));
  expect(sealBlindBox.mock.calls.map((call) => (call as unknown[])[2])).toEqual([
    { purpose: 'seal', name: 'LOGIN' }, { purpose: 'seal', name: 'LOGIN.username' }, { purpose: 'seal', name: 'LOGIN' },
  ]);
});

it('requires a unique populated extra field, saves it under its record name, and confirms removal', async () => {
  renderEditor();
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'username');
  change('New field value', 'seed');
  expect((screen.getByRole('button', { name: 'Save field' }) as HTMLButtonElement).disabled).toBe(true);
  change('New field name', 'totp');
  api.updateVaultSecretField.mockResolvedValue({ ok: true, secret: { ...secret, fields: ['password', 'username', 'totp'], updated_at: 'version-2' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save field' }));
  await screen.findByLabelText('Value for totp');
  expect(sealBlindBox).toHaveBeenCalledWith('seed', { public_key: 'public' }, { purpose: 'seal', name: 'LOGIN.totp' });
  expect(screen.queryByRole('button', { name: 'Remove password' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Remove totp' }));
  expect(api.deleteVaultSecretField).not.toHaveBeenCalled();
  api.deleteVaultSecretField.mockResolvedValue({ ok: true, secret: { ...secret, updated_at: 'version-3' } });
  fireEvent.click(screen.getByRole('button', { name: en.vaults.delete }));
  await waitFor(() => expect(api.deleteVaultSecretField).toHaveBeenCalledWith('LOGIN', 'totp'));
  await waitFor(() => expect(screen.queryByLabelText('Value for totp')).toBeNull());
});

it.each(['response', 'exception'])('refreshes a conflicting version without retrying, then permits a new save (%s)', async (failure) => {
  const fresh = { ...secret, updated_at: 'version-9' };
  if (failure === 'exception') {
    api.updateVaultSecretField.mockRejectedValueOnce(new ApiError('stale version', 409, 'secret_conflict'));
  } else {
    api.updateVaultSecretField.mockResolvedValueOnce({ ok: false, code: 'secret_conflict' });
  }
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [fresh] });
  api.updateVaultSecretField.mockResolvedValue({ ok: true, secret: { ...fresh, updated_at: 'version-10' } });
  renderEditor();
  change('Value for username', 'new-user');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  expect((await screen.findByRole('alert')).textContent).toBe(en.vaults.fields.secretConflict);
  expect(api.listVaultSecrets).toHaveBeenCalledWith({ cache: false });
  expect(api.updateVaultSecretField).toHaveBeenCalledOnce();
  expect(screen.queryByRole('status')).toBeNull();
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe('new-user');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await screen.findByRole('status');
  expect(api.updateVaultSecretField).toHaveBeenCalledTimes(2);
  expect(api.updateVaultSecretField.mock.calls[1][2].expected_updated_at).toBe('version-9');
});

it('seals a protected replacement with its field record and drops its entered value', async () => {
  const envelope = { ciphertext: 'ciphertext', nonce: 'nonce', wrap_meta: '{}' };
  vault.sealValue.mockImplementation(async (_name, _kind, value) => { value.clear(); return { envelope }; });
  renderEditor({ ...secret, protection: 'protected' });
  change('Value for username', '私密用户');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledWith(
    'LOGIN', 'username', { record: 'LOGIN.username', sealed: envelope, expected_updated_at: 'version-1' }, { handleError: false },
  ));
  expect(vault.sealValue.mock.calls[0].slice(0, 2)).toEqual(['LOGIN.username', 'static']);
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe('');
  expect(sealBlindBox).not.toHaveBeenCalled();
});

it('re-reads metadata and re-seals exactly once after a record mismatch', async () => {
  const renamed = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' };
  api.updateVaultSecretField
    .mockResolvedValueOnce({ ok: false, code: 'record_mismatch', message: 'stale record' })
    .mockResolvedValueOnce({ ok: true, secret: { ...renamed, updated_at: 'version-3' } });
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [renamed] });
  renderEditor();
  change('Value for password', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledTimes(2));
  expect(api.listVaultSecrets).toHaveBeenCalledWith({ cache: false });
  expect(api.updateVaultSecretField.mock.calls).toEqual([
    ['LOGIN', 'password', { record: 'LOGIN', blind_box: box, expected_updated_at: 'version-1' }, { handleError: false }],
    ['LOGIN', 'credential', { record: 'LOGIN', blind_box: box, expected_updated_at: 'version-2' }, { handleError: false }],
  ]);
  expect(sealBlindBox).toHaveBeenCalledTimes(2);
  expect((screen.getByLabelText('Value for credential') as HTMLInputElement).value).toBe('');
  expect(screen.queryByRole('alert')).toBeNull();
});

it('surfaces an extra-field record conflict without retrying under a changed default label', async () => {
  const changed = { ...secret, default_field: 'username', fields: ['username'], updated_at: 'version-2' };
  api.updateVaultSecretField.mockResolvedValueOnce({ ok: false, code: 'record_mismatch', message: 'stale record' });
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [changed] });
  renderEditor();
  change('Value for username', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
  expect(api.updateVaultSecretField).toHaveBeenCalledOnce();
  expect(sealBlindBox).toHaveBeenCalledOnce();
});

it('retries once when the API client throws ApiError for record_mismatch', async () => {
  api.updateVaultSecretField
    .mockRejectedValueOnce(new ApiError('stale record', 409, 'record_mismatch'))
    .mockResolvedValueOnce({ ok: true, secret: { ...secret, updated_at: 'version-2' } });
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [secret] });
  renderEditor();
  change('Value for username', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledTimes(2));
  expect(api.listVaultSecrets).toHaveBeenCalledWith({ cache: false });
  expect(api.updateVaultSecretField.mock.calls[1][2]).toMatchObject({ record: 'LOGIN.username', expected_updated_at: 'version-1' });
});
