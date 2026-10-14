// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ApiError, type VaultSecret } from '@/context/ApiContext';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { VaultSecretFieldsEditor } from './vault-secret-fields-editor';

const api = vi.hoisted(() => ({
  updateVaultSecret: vi.fn(), updateVaultSecretField: vi.fn(), deleteVaultSecretField: vi.fn(),
  listVaultSecrets: vi.fn(),
  connectWorkbenchEvents: vi.fn(() => vi.fn()),
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
  lng: 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false },
});
const secret: VaultSecret = {
  name: 'LOGIN', default_field: 'password', fields: ['password', 'username'],
  kind: 'static', protection: 'standard', signer_kind: null, source: 'user', tags: [],
  policy: {}, last_used_at: null, use_count: 0, created_at: 'created', updated_at: 'version-1',
};
type EventHandlers = {
  onConnected?: () => void;
  onVaultsUpdated?: (data: { scope: string; secret_name?: string }) => void;
};
let eventHandlers: EventHandlers | null = null;
const change = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}
const renderEditor = (entry = secret) => render(
  <I18nextProvider i18n={i18n}><VaultSecretFieldsEditor secret={entry} disabled={false} onBusyChange={vi.fn()} /></I18nextProvider>,
);
beforeEach(() => {
  api.updateVaultSecret.mockReset();
  api.updateVaultSecretField.mockReset();
  api.deleteVaultSecretField.mockReset();
  api.listVaultSecrets.mockReset();
  api.connectWorkbenchEvents.mockReset();
  api.connectWorkbenchEvents.mockImplementation((handlers: EventHandlers) => {
    eventHandlers = handlers;
    return vi.fn();
  });
  api.updateVaultSecret.mockReset();
  api.updateVaultSecretField.mockResolvedValue({ ok: true, secret: { ...secret, updated_at: 'version-2' } });
  api.deleteVaultSecretField.mockReset();
  api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: [secret] });

});
afterEach(async () => { cleanup(); eventHandlers = null; vi.clearAllMocks(); await i18n.changeLanguage('en'); });

// The editor owns metadata versions between browser encryptions. Form creation and crypto
// tests cannot catch a second field write reusing the first version, or a default rename
// accidentally turning its replacement into a dotted record.
it('updates envelope versions after replacing a field and renaming the default label', async () => {
  const rendered = renderEditor();
  change('Value for password', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledWith(
    'LOGIN', 'password', { record: 'LOGIN', blind_box: box, expected_updated_at: 'version-1' }, { handleError: false },
  ));
  await screen.findByRole('status');
  expect((screen.getByLabelText('Value for password') as HTMLInputElement).value).toBe('');
  // A parent poll can still hand back the old object after the child accepted its own
  // successful write. The editor re-reads authoritative metadata instead of adopting it.
  api.listVaultSecrets.mockResolvedValue({
    ok: true,
    secrets: [{ ...secret, updated_at: 'version-2' }],
  });
  rendered.rerender(
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={{ ...secret }} disabled={false} onBusyChange={vi.fn()} />
    </I18nextProvider>,
  );
  expect(screen.getByLabelText('Value for username')).toBeTruthy();
  change('Value for username', 'next-user');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenLastCalledWith(
    'LOGIN', 'username', { record: 'LOGIN.username', blind_box: box, expected_updated_at: 'version-2' }, { handleError: false },
  ));
  await waitFor(() => expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe(''));
  const beforeRename = { ...secret, updated_at: 'version-2' };
  api.listVaultSecrets.mockResolvedValue({
    ok: true,
    secrets: [{ ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' }],
  });
  rendered.rerender(
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={beforeRename} disabled={false} onBusyChange={vi.fn()} />
    </I18nextProvider>,
  );
  api.updateVaultSecret.mockResolvedValue({ ok: true, secret: { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' } });
  change('Default field name', 'credential');
  fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
  await screen.findByLabelText('Value for credential');
  expect(api.updateVaultSecret).toHaveBeenCalledWith('LOGIN', { default_field: 'credential' }, { handleError: false });
  rendered.rerender(
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={{ ...beforeRename }} disabled={false} onBusyChange={vi.fn()} />
    </I18nextProvider>,
  );
  expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('credential');
  api.updateVaultSecretField.mockResolvedValue({ ok: true, secret: { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-3' } });
  change('Value for credential', 'next');
  fireEvent.click(screen.getByRole('button', { name: 'Save credential' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenLastCalledWith(
    'LOGIN', 'credential', { record: 'LOGIN', blind_box: box, expected_updated_at: 'version-2' }, { handleError: false },
  ));
  expect(sealBlindBox.mock.calls.map((call) => (call as unknown[])[2])).toEqual([
    { purpose: 'seal', name: 'LOGIN' }, { purpose: 'seal', name: 'LOGIN.username' }, { purpose: 'seal', name: 'LOGIN' },
  ]);
});

// A row has one Save. On the default row it renames first (metadata only) and then seals the typed
// value under the renamed field's bare record; sealing against the pre-rename metadata would fail
// the field check and write nothing. Only the default field's name is editable.
it('renames the default field and then saves its typed value under the new name', async () => {
  const renamed = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' };
  api.updateVaultSecret.mockResolvedValue({ ok: true, secret: renamed });
  api.updateVaultSecretField.mockResolvedValue({ ok: true, secret: { ...renamed, updated_at: 'version-3' } });
  renderEditor();
  expect(screen.getAllByRole('textbox').filter((input) => input.getAttribute('aria-label') === 'Default field name')).toHaveLength(1);
  expect(screen.getByTitle(en.vaults.fields.extraNotRenamable).textContent).toBe('username');
  expect(screen.queryByRole('button', { name: 'Save password' })).toBeNull();
  change('Value for password', 'typed-default');
  change('Default field name', 'credential');
  fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledWith(
    'LOGIN', 'credential', { record: 'LOGIN', blind_box: box, expected_updated_at: 'version-2' }, { handleError: false },
  ));
  expect(api.updateVaultSecret).toHaveBeenCalledWith('LOGIN', { default_field: 'credential' }, { handleError: false });
  expect(api.updateVaultSecret.mock.invocationCallOrder[0]).toBeLessThan(api.updateVaultSecretField.mock.invocationCallOrder[0]);
  expect(sealBlindBox).toHaveBeenCalledWith('typed-default', { public_key: 'public' }, { purpose: 'seal', name: 'LOGIN' });
  await waitFor(() => expect((screen.getByLabelText('Value for credential') as HTMLInputElement).value).toBe(''));
  expect(screen.queryByRole('button', { name: 'Save credential' })).toBeNull();
});

it('keeps a typed default label when saving another field', async () => {
  api.updateVaultSecretField.mockResolvedValue({
    ok: true,
    secret: { ...secret, updated_at: 'version-2' },
  });
  renderEditor();
  change('Default field name', 'credential');
  change('Value for username', 'typed-extra');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await screen.findByRole('status');
  expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('credential');
});

it('accepts a server refetch after a legitimate quick rename back before parent acknowledgement', async () => {
  const renamedByUser = { ...secret, default_field: 'password', fields: ['password', 'username'] };
  const renamedBackExternally = { ...secret, default_field: 'value', fields: ['value', 'username'] };
  api.updateVaultSecret.mockResolvedValueOnce({ ok: true, secret: renamedByUser });
  const rendered = renderEditor({ ...renamedBackExternally });
  change('Default field name', 'password');
  fireEvent.click(screen.getByRole('button', { name: 'Save value' }));
  await waitFor(() => expect(api.updateVaultSecret).toHaveBeenCalledWith('LOGIN', { default_field: 'password' }, { handleError: false }));
  await waitFor(() => expect(screen.getByLabelText('Value for password')).toBeTruthy());

  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [renamedBackExternally] });
  rendered.rerender(
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={renamedBackExternally} disabled={false} onBusyChange={vi.fn()} />
    </I18nextProvider>,
  );
  await waitFor(() => expect(api.listVaultSecrets).toHaveBeenCalledWith({ cache: false }));
  expect((screen.getAllByLabelText('Default field name').at(-1) as HTMLInputElement).value).toBe('value');
});

it('ignores an older refetch when a newer authoritative read finishes first', async () => {
  const older = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  const newer = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  api.listVaultSecrets.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
  renderEditor();
  const newerShape = { ...secret, default_field: 'new', fields: ['new', 'username'], updated_at: 'version-3' };
  await act(async () => {
    eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' });
    eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' });
  });
  await act(async () => { newer.resolve({ ok: true, secrets: [newerShape] }); await newer.promise; });
  await waitFor(() => expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('new'));
  await act(async () => { older.reject(new Error('older read failed')); await older.promise.catch(() => undefined); });
  expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('new');
  expect(screen.queryByLabelText('Value for old')).toBeNull();
  expect(screen.queryByRole('alert')).toBeNull();
});

it('invalidates an older read when its own write becomes authoritative', async () => {
  const older = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  api.listVaultSecrets.mockReturnValueOnce(older.promise);
  renderEditor();
  await act(async () => {
    eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' });
  });

  const ownWrite = { ...secret, updated_at: 'version-2' };
  api.updateVaultSecretField.mockResolvedValueOnce({ ok: true, secret: ownWrite });
  change('Value for password', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
  await screen.findByRole('status');
  await act(async () => { older.resolve({ ok: true, secrets: [secret] }); await older.promise; });

  api.updateVaultSecretField.mockResolvedValueOnce({ ok: true, secret: { ...ownWrite, updated_at: 'version-3' } });
  change('Value for username', 'next-user');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenLastCalledWith(
    'LOGIN', 'username', { record: 'LOGIN.username', blind_box: box, expected_updated_at: 'version-2' }, { handleError: false },
  ));
});

it('catches up after an authoritative read lands while a write is in flight', async () => {
  const read = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  const write = deferred<{ ok: boolean; secret: VaultSecret }>();
  const newerTruth = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' };
  api.listVaultSecrets.mockReturnValueOnce(read.promise).mockResolvedValueOnce({ ok: true, secrets: [newerTruth] });
  api.updateVaultSecretField.mockReturnValueOnce(write.promise);
  renderEditor();
  await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
  change('Value for password', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledOnce());
  await act(async () => { read.resolve({ ok: true, secrets: [newerTruth] }); await read.promise; });
  await waitFor(() => expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('credential'));
  write.resolve({ ok: true, secret: { ...secret, updated_at: 'version-2' } });
  await waitFor(() => expect(api.listVaultSecrets).toHaveBeenCalledTimes(2));
  expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('credential');
});

it.each(['connected', 'updated'] as const)('refetches authoritative truth on vaults.%s', async (event) => {
  const refreshed = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' };
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [refreshed] });
  renderEditor();
  await act(async () => {
    if (event === 'connected') eventHandlers?.onConnected?.();
    else eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' });
  });
  await waitFor(() => expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('credential'));
  expect(api.listVaultSecrets).toHaveBeenCalledWith({ cache: false });
});

it('drops removed drafts and shows a localized notice', async () => {
  api.listVaultSecrets.mockResolvedValueOnce({
    ok: true,
    secrets: [{ ...secret, fields: ['password'], updated_at: 'version-2' }],
  });
  renderEditor();
  change('Value for username', 'draft-user');
  await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('username'));
  expect(screen.queryByLabelText('Value for username')).toBeNull();
});

it.each(['constructor', 'tostring', 'valueof'])('treats %s as a normal field key', async (field) => {
  const entry = { ...secret, fields: ['password', field] };
  api.updateVaultSecretField.mockResolvedValue({
    ok: true,
    secret: { ...entry, updated_at: 'version-2' },
  });
  renderEditor(entry);
  const value = screen.getByLabelText(`Value for ${field}`) as HTMLInputElement;
  expect(value.value).toBe('');
  expect(screen.queryByRole('button', { name: `Save ${field}` })).toBeNull();
  fireEvent.change(value, { target: { value: 'typed-value' } });
  const save = screen.getByRole('button', { name: `Save ${field}` }) as HTMLButtonElement;
  expect(save.disabled).toBe(false);
  fireEvent.click(save);
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledWith(
    'LOGIN',
    field,
    { record: `LOGIN.${field}`, blind_box: box, expected_updated_at: 'version-1' },
    { handleError: false },
  ));
  expect(sealBlindBox).toHaveBeenCalledWith(
    'typed-value',
    { public_key: 'public' },
    { purpose: 'seal', name: `LOGIN.${field}` },
  );
});

it.each(['version-1', 'version-2'])('adopts current labels at %s while preserving default and extra drafts', async (updated_at) => {
  const { rerender } = renderEditor();
  change('Value for password', 'keep-default-draft');
  change('Value for username', 'keep-this-draft');
  const renamed = {
    ...secret,
    default_field: 'credential',
    fields: ['credential', 'username'],
    updated_at,
  };
  api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: [renamed] });
  rerender(
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={renamed} disabled={false} onBusyChange={vi.fn()} />
    </I18nextProvider>,
  );
  await waitFor(() => expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('credential'));
  expect((screen.getByLabelText('Value for credential') as HTMLInputElement).value).toBe('keep-default-draft');
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe('keep-this-draft');
});

it('warns and disables field mutations when field metadata is corrupt', () => {
  renderEditor({ ...secret, fields_state: 'corrupt' });
  expect(screen.getByRole('alert').textContent).toBe(en.errors.fields_corrupt);
  expect((screen.getByLabelText('Default field name') as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText('Value for password') as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole('button', { name: /^Save / })).toBeNull();
  expect((screen.getByRole('button', { name: 'Remove username' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Add field' }) as HTMLButtonElement).disabled).toBe(true);
});

it('adopts a same-version corruption state from refreshed metadata', async () => {
  api.listVaultSecrets.mockResolvedValueOnce({
    ok: true,
    secrets: [{ ...secret, fields_state: 'corrupt' }],
  });
  const { rerender } = renderEditor();
  rerender(
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={{ ...secret, fields_state: 'corrupt' }} disabled={false} onBusyChange={vi.fn()} />
    </I18nextProvider>,
  );
  await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(en.errors.fields_corrupt));
  expect((screen.getByLabelText('Value for password') as HTMLInputElement).disabled).toBe(true);
});

it('requires a unique populated extra field, saves it under its record name, and confirms removal', async () => {
  renderEditor();
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'username');
  change('New field value', 'seed');
  expect(screen.getByText("There's already a username field")).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Save new field' }) as HTMLButtonElement).disabled).toBe(true);
  change('New field name', 'totp');
  api.updateVaultSecretField.mockResolvedValue({ ok: true, secret: { ...secret, fields: ['password', 'username', 'totp'], updated_at: 'version-2' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save new field' }));
  await screen.findByLabelText('Value for totp');
  expect(screen.queryByLabelText('New field name')).toBeNull();
  expect(sealBlindBox).toHaveBeenCalledWith('seed', { public_key: 'public' }, { purpose: 'seal', name: 'LOGIN.totp' });
  expect(screen.queryByRole('button', { name: 'Remove password' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Remove totp' }));
  expect(screen.getByText(/and its value\?/).textContent).toBe('Remove totp and its value? Existing grants will expire.');
  expect(api.deleteVaultSecretField).not.toHaveBeenCalled();
  api.deleteVaultSecretField.mockResolvedValue({ ok: true, secret: { ...secret, updated_at: 'version-3' } });
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  await waitFor(() => expect(api.deleteVaultSecretField).toHaveBeenCalledWith('LOGIN', 'totp', { expected_updated_at: 'version-2' }));
  await waitFor(() => expect(screen.queryByLabelText('Value for totp')).toBeNull());
});

it('refreshes a stale delete version without removing the newer field or retrying', async () => {
  api.deleteVaultSecretField.mockRejectedValueOnce(new ApiError('stale delete', 409, 'secret_conflict'));
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [{ ...secret, updated_at: 'version-8' }] });
  renderEditor();
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect((await screen.findByRole('alert')).textContent).toBe(en.vaults.fields.secretConflict);
  expect(api.deleteVaultSecretField).toHaveBeenCalledOnce();
  expect(screen.getByLabelText('Value for username')).toBeTruthy();
  api.deleteVaultSecretField.mockResolvedValueOnce({ ok: true, secret: { ...secret, fields: ['password'], updated_at: 'version-9' } });
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  await waitFor(() => expect(screen.queryByLabelText('Value for username')).toBeNull());
  expect(api.deleteVaultSecretField.mock.calls).toEqual([
    ['LOGIN', 'username', { expected_updated_at: 'version-1' }],
    ['LOGIN', 'username', { expected_updated_at: 'version-8' }],
  ]);
});

it('renders a Chinese field validation error from the stable code', async () => {
  await i18n.changeLanguage('zh');
  api.updateVaultSecretField.mockResolvedValueOnce({ ok: false, code: 'field_not_found', message: 'field not found on LOGIN' });
  renderEditor();
  change(i18n.t('vaults.fields.value', { field: 'username' }), 'replacement');
  fireEvent.click(screen.getByRole('button', { name: i18n.t('vaults.fields.saveNamed', { field: 'username' }) }));
  expect((await screen.findByRole('alert')).textContent).toBe(zh.errors.field_not_found);
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

it('retries a record mismatch with the superseded read entry', async () => {
  const retryRead = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  const propRead = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  const renamed = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' };
  api.updateVaultSecretField
    .mockResolvedValueOnce({ ok: false, code: 'record_mismatch', message: 'stale record' })
    .mockResolvedValueOnce({ ok: true, secret: { ...renamed, updated_at: 'version-3' } });
  api.listVaultSecrets.mockReturnValueOnce(retryRead.promise).mockReturnValueOnce(propRead.promise);
  const rendered = renderEditor();
  change('Value for password', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
  await waitFor(() => expect(api.listVaultSecrets).toHaveBeenCalledTimes(1));
  rendered.rerender(
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={{ ...secret, updated_at: 'version-2' }} disabled={false} onBusyChange={vi.fn()} />
    </I18nextProvider>,
  );
  await waitFor(() => expect(api.listVaultSecrets).toHaveBeenCalledTimes(2));
  await act(async () => {
    propRead.resolve({ ok: true, secrets: [renamed] });
    retryRead.resolve({ ok: true, secrets: [renamed] });
    await Promise.all([propRead.promise, retryRead.promise]);
  });
  await waitFor(() => expect(api.updateVaultSecretField).toHaveBeenCalledTimes(2));
  expect(api.updateVaultSecretField.mock.calls[1][2].expected_updated_at).toBe('version-2');
});

it('keeps a secret conflict after its refresh is superseded', async () => {
  const conflictRead = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  const propRead = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  api.updateVaultSecretField.mockResolvedValueOnce({ ok: false, code: 'secret_conflict' });
  api.listVaultSecrets.mockReturnValueOnce(conflictRead.promise).mockReturnValueOnce(propRead.promise);
  const rendered = renderEditor();
  change('Value for username', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  await waitFor(() => expect(api.listVaultSecrets).toHaveBeenCalledTimes(1));
  rendered.rerender(
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={{ ...secret, updated_at: 'version-2' }} disabled={false} onBusyChange={vi.fn()} />
    </I18nextProvider>,
  );
  await waitFor(() => expect(api.listVaultSecrets).toHaveBeenCalledTimes(2));
  await act(async () => {
    propRead.resolve({ ok: true, secrets: [secret] });
    conflictRead.resolve({ ok: true, secrets: [secret] });
    await Promise.all([propRead.promise, conflictRead.promise]);
  });
  expect((await screen.findByRole('alert')).textContent).toBe(en.vaults.fields.secretConflict);
});

it('shows a missing-secret error when a conflict refresh finds no secret', async () => {
  api.updateVaultSecretField.mockResolvedValueOnce({ ok: false, code: 'secret_conflict' });
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [] });
  renderEditor();
  change('Value for username', 'replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Save username' }));
  expect((await screen.findByRole('alert')).textContent).toBe(en.errors.secret_not_found);
});

it('does not report a dropped draft when its field is cleared before own removal', async () => {
  api.deleteVaultSecretField.mockResolvedValueOnce({ ok: true, secret: { ...secret, fields: ['password'], updated_at: 'version-2' } });
  renderEditor();
  change('Value for username', 'temporary');
  change('Value for username', '');
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  await waitFor(() => expect(screen.queryByLabelText('Value for username')).toBeNull());
  expect(screen.queryByText(en.vaults.fields.draftsDropped.replace('{{fields}}', 'username'))).toBeNull();
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
