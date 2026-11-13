// @vitest-environment jsdom
import { webcrypto } from 'node:crypto';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ApiError, type VaultSecret } from '@/context/ApiContext';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { VaultSecretFieldsEditor, type VaultFieldsCommit } from './vault-secret-fields-editor';

const api = vi.hoisted(() => ({
  updateVaultSecret: vi.fn(), updateVaultSecretFields: vi.fn(),
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
// The dialog's Save, as vault-secret-form drives it: commit through the editor's handle.
const commitRef: { current: VaultFieldsCommit | null } = { current: null };
const states: Array<{ dirty: boolean; valid: boolean }> = [];
const editor = (entry: VaultSecret) => (
  <I18nextProvider i18n={i18n}>
    <VaultSecretFieldsEditor secret={entry} disabled={false} onBusyChange={vi.fn()} commitRef={commitRef}
      onChange={(state) => states.push(state)} />
  </I18nextProvider>
);
const renderEditor = (entry = secret) => render(editor(entry));
const lastState = () => states[states.length - 1];
let commitResult: boolean | null = null;
const save = async () => {
  await act(async () => { commitResult = await commitRef.current!.commit(); });
  return commitResult;
};
const batch = (n = 0) => api.updateVaultSecretFields.mock.calls[n];
beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('isSecureContext', true);
  api.updateVaultSecret.mockReset();
  api.updateVaultSecretFields.mockReset();
  api.listVaultSecrets.mockReset();
  states.length = 0;
  commitResult = null;
  api.connectWorkbenchEvents.mockReset();
  api.connectWorkbenchEvents.mockImplementation((handlers: EventHandlers) => {
    eventHandlers = handlers;
    return vi.fn();
  });
  api.updateVaultSecretFields.mockResolvedValue({ ok: true, secret: { ...secret, updated_at: 'version-2' } });
  api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: [secret] });

});
afterEach(async () => { cleanup(); eventHandlers = null; vi.clearAllMocks(); vi.unstubAllGlobals(); await i18n.changeLanguage('en'); });


const box2 = (name: string) => ({ purpose: 'seal', name });

it('saves nothing and reports clean when nothing changed', async () => {
  renderEditor();
  expect(lastState()).toEqual({ dirty: false, valid: true });
  expect(await save()).toBe(true);
  expect(api.updateVaultSecretFields).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: /^Save/ })).toBeNull();
});

// Every change is a draft until the dialog's Save, which commits them all in one request: the
// default's rename and value (under the new label's bare record), an extra's value, a new field,
// and a removed field.
it('commits a rename, values, an added field and a removal in one atomic request', async () => {
  const entry = { ...secret, fields: ['password', 'username', 'totp'] };
  const after = { ...secret, default_field: 'credential', fields: ['credential', 'username', 'note'], updated_at: 'version-2' };
  api.updateVaultSecretFields.mockResolvedValueOnce({ ok: true, secret: after });
  renderEditor(entry);
  change('Default field name', 'credential');
  change('Value for password', 'typed-default');
  change('Value for username', 'typed-user');
  fireEvent.click(screen.getByRole('button', { name: 'Remove totp' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect(screen.queryByLabelText('Value for totp')).toBeNull();
  expect(screen.getByRole('status').textContent).toContain('totp');
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'note');
  change('New field value', 'typed-note');
  expect(lastState()).toEqual({ dirty: true, valid: true });
  expect(api.updateVaultSecretFields).not.toHaveBeenCalled();
  expect(await save()).toBe(true);
  expect(api.updateVaultSecretFields).toHaveBeenCalledOnce();
  expect(batch()).toEqual(['LOGIN', {
    expected_updated_at: 'version-1',
    default_field: 'credential',
    values: [
      { field: 'credential', record: 'LOGIN', blind_box: box },
      { field: 'username', record: 'LOGIN.username', blind_box: box },
      { field: 'note', record: 'LOGIN.note', blind_box: box },
    ],
    remove: ['totp'],
  }, { handleError: false }]);
  expect(sealBlindBox.mock.calls.map((call) => (call as unknown[])[2])).toEqual([box2('LOGIN'), box2('LOGIN.username'), box2('LOGIN.note')]);
  await waitFor(() => expect((screen.getByLabelText('Value for credential') as HTMLInputElement).value).toBe(''));
  expect(screen.queryByLabelText('New field name')).toBeNull();
  expect(lastState().dirty).toBe(false);
});

it('can undo a staged removal before saving', () => {
  renderEditor();
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect(screen.queryByLabelText('Value for username')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.undoRemove }));
  expect(screen.getByLabelText('Value for username')).toBeTruthy();
  expect(lastState().dirty).toBe(false);
});

it('is not valid while the default rename collides or a new field is incomplete', () => {
  renderEditor();
  change('Default field name', 'username');
  expect(screen.getByText("There's already a username field")).toBeTruthy();
  expect(lastState()).toEqual({ dirty: true, valid: false });
  change('Default field name', 'password');
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'username');
  change('New field value', 'seed');
  expect(lastState().valid).toBe(false);
  change('New field name', 'totp');
  expect(lastState()).toEqual({ dirty: true, valid: true });
});

// A new field named like the default's drafted new name would be sealed as the default's record
// and overwrite it on the server; it is a name collision, both ways round.
it('rejects a new field named like the default field’s new name', async () => {
  renderEditor();
  change('Default field name', 'credential');
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'credential');
  change('New field value', 'seed');
  expect(screen.getAllByText("There's already a credential field").length).toBeGreaterThan(0);
  expect(lastState().valid).toBe(false);
  expect(await save()).toBe(false);
  expect(api.updateVaultSecretFields).not.toHaveBeenCalled();
  change('New field name', 'note');
  expect(lastState().valid).toBe(true);
});

// Entry gating must stop ciphertext writes, not metadata management.
it('blocks protected value writes before refresh but permits a metadata-only rename on LAN HTTP', async () => {
  vi.stubGlobal('location', new URL('http://192.0.2.1:5123'));
  vi.stubGlobal('isSecureContext', false);
  const protectedSecret = { ...secret, protection: 'protected' };
  api.updateVaultSecretFields.mockResolvedValue({ ok: true, secret: { ...protectedSecret, default_field: 'credential', fields: ['credential', 'username'] } });
  renderEditor(protectedSecret);
  change('Value for password', 'public-test-draft');
  expect(lastState().valid).toBe(false);
  expect(vault.refresh).not.toHaveBeenCalled();
  change('Value for password', '');
  change('Default field name', 'credential');
  expect(lastState().valid).toBe(true);
  expect(await save()).toBe(true);
  expect(batch()[1]).toEqual({ expected_updated_at: 'version-1', default_field: 'credential', values: [], remove: [] });
  expect(vault.sealValue).not.toHaveBeenCalled();
});

it('seals protected values in the sandbox under their records and drops the page copies', async () => {
  const envelope = { ciphertext: 'ciphertext', nonce: 'nonce', wrap_meta: '{}' };
  vault.sealValue.mockImplementation(async (_name, _kind, value) => { value.clear(); return { envelope }; });
  renderEditor({ ...secret, protection: 'protected' });
  change('Value for password', 'secret-pass');
  change('Value for username', '私密用户');
  expect(await save()).toBe(true);
  expect(vault.sealValue.mock.calls.map((call) => (call as unknown[]).slice(0, 2))).toEqual([['LOGIN', 'static'], ['LOGIN.username', 'static']]);
  expect(batch()[1].values).toEqual([
    { field: 'password', record: 'LOGIN', sealed: envelope },
    { field: 'username', record: 'LOGIN.username', sealed: envelope },
  ]);
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe('');
  expect(sealBlindBox).not.toHaveBeenCalled();
});

it('re-reads metadata and re-seals exactly once after a record mismatch', async () => {
  const renamed = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' };
  api.updateVaultSecretFields
    .mockResolvedValueOnce({ ok: false, code: 'record_mismatch', message: 'stale record' })
    .mockResolvedValueOnce({ ok: true, secret: { ...renamed, updated_at: 'version-3' } });
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [renamed] });
  renderEditor();
  change('Value for password', 'replacement');
  expect(await save()).toBe(true);
  expect(api.updateVaultSecretFields.mock.calls.map((call) => call[1])).toEqual([
    { expected_updated_at: 'version-1', values: [{ field: 'password', record: 'LOGIN', blind_box: box }], remove: [] },
    { expected_updated_at: 'version-2', values: [{ field: 'credential', record: 'LOGIN', blind_box: box }], remove: [] },
  ]);
  expect(sealBlindBox).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole('alert')).toBeNull();
});

it('retries once when the API client throws ApiError for record_mismatch, and no more', async () => {
  api.updateVaultSecretFields
    .mockRejectedValueOnce(new ApiError('stale record', 409, 'record_mismatch'))
    .mockRejectedValueOnce(new ApiError('stale record', 409, 'record_mismatch'));
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [secret] });
  renderEditor();
  change('Value for username', 'replacement');
  expect(await save()).toBe(false);
  expect(api.updateVaultSecretFields).toHaveBeenCalledTimes(2);
  expect((await screen.findByRole('alert')).textContent).toBe(en.vaults.fields.recordConflict);
});

it('refuses to write an extra field’s draft into the default when another write made it the default', async () => {
  const changed = { ...secret, default_field: 'username', fields: ['username'], updated_at: 'version-2' };
  api.updateVaultSecretFields.mockResolvedValueOnce({ ok: false, code: 'record_mismatch', message: 'stale record' });
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [changed] });
  renderEditor();
  change('Value for username', 'replacement');
  expect(await save()).toBe(false);
  expect(api.updateVaultSecretFields).toHaveBeenCalledOnce();
  expect(sealBlindBox).toHaveBeenCalledOnce();
  expect(screen.getByRole('alert')).toBeTruthy();
});

it.each(['response', 'exception'])('refreshes a conflicting version without retrying, keeps the drafts, then saves (%s)', async (failure) => {
  const fresh = { ...secret, updated_at: 'version-9' };
  if (failure === 'exception') {
    api.updateVaultSecretFields.mockRejectedValueOnce(new ApiError('stale version', 409, 'secret_conflict'));
  } else {
    api.updateVaultSecretFields.mockResolvedValueOnce({ ok: false, code: 'secret_conflict' });
  }
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [fresh] });
  api.updateVaultSecretFields.mockResolvedValue({ ok: true, secret: { ...fresh, updated_at: 'version-10' } });
  renderEditor();
  change('Value for username', 'new-user');
  expect(await save()).toBe(false);
  expect((await screen.findByRole('alert')).textContent).toBe(en.vaults.fields.secretConflict);
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe('new-user');
  expect(await save()).toBe(true);
  expect(batch(1)[1].expected_updated_at).toBe('version-9');
});

it('shows a missing-secret error when a conflict refresh finds no secret', async () => {
  api.updateVaultSecretFields.mockResolvedValueOnce({ ok: false, code: 'secret_conflict' });
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [] });
  renderEditor();
  change('Value for username', 'replacement');
  expect(await save()).toBe(false);
  expect((await screen.findByRole('alert')).textContent).toBe(en.errors.secret_not_found);
});

it('renders a Chinese field validation error from the stable code', async () => {
  await i18n.changeLanguage('zh');
  api.updateVaultSecretFields.mockResolvedValueOnce({ ok: false, code: 'field_not_found', message: 'field not found on LOGIN' });
  renderEditor();
  change(i18n.t('vaults.fields.value', { field: 'username' }), 'replacement');
  expect(await save()).toBe(false);
  expect((await screen.findByRole('alert')).textContent).toBe(zh.errors.field_not_found);
});

it('warns and disables field changes when field metadata is corrupt', async () => {
  renderEditor({ ...secret, fields_state: 'corrupt' });
  expect(screen.getByRole('alert').textContent).toBe(en.errors.fields_corrupt);
  expect((screen.getByLabelText('Value for password') as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Remove username' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Add field' }) as HTMLButtonElement).disabled).toBe(true);
  expect(lastState().valid).toBe(false);
});

it.each(['constructor', 'tostring', 'valueof'])('treats %s as a normal field key', async (field) => {
  const entry = { ...secret, fields: ['password', field] };
  renderEditor(entry);
  change(`Value for ${field}`, 'typed-value');
  expect(await save()).toBe(true);
  expect(batch()[1].values).toEqual([{ field, record: `LOGIN.${field}`, blind_box: box }]);
});

it('catches up after an authoritative read lands while the commit is in flight', async () => {
  const read = deferred<{ ok: boolean; secrets: VaultSecret[] }>();
  const write = deferred<{ ok: boolean; secret: VaultSecret }>();
  const newerTruth = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' };
  api.listVaultSecrets.mockReturnValueOnce(read.promise).mockResolvedValueOnce({ ok: true, secrets: [newerTruth] });
  api.updateVaultSecretFields.mockReturnValueOnce(write.promise);
  renderEditor();
  await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
  change('Value for username', 'replacement');
  let done: Promise<boolean> | null = null;
  await act(async () => { done = commitRef.current!.commit(); });
  await waitFor(() => expect(api.updateVaultSecretFields).toHaveBeenCalledOnce());
  await act(async () => { read.resolve({ ok: true, secrets: [newerTruth] }); await read.promise; });
  await act(async () => { write.resolve({ ok: true, secret: { ...secret, updated_at: 'version-2' } }); await done; });
  await waitFor(() => expect(api.listVaultSecrets).toHaveBeenCalledTimes(2));
  expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('credential');
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
  expect(screen.queryByRole('alert')).toBeNull();
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
});

it('drops drafts of fields removed elsewhere and says so', async () => {
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [{ ...secret, fields: ['password'], updated_at: 'version-2' }] });
  renderEditor();
  change('Value for username', 'draft-user');
  await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('username'));
  expect(screen.queryByLabelText('Value for username')).toBeNull();
});

it.each(['version-1', 'version-2'])('adopts current labels at %s while preserving default and extra drafts', async (updated_at) => {
  const { rerender } = renderEditor();
  change('Value for password', 'keep-default-draft');
  change('Value for username', 'keep-this-draft');
  const renamed = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at };
  api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: [renamed] });
  rerender(editor(renamed));
  await waitFor(() => expect((screen.getByLabelText('Default field name') as HTMLInputElement).value).toBe('credential'));
  expect((screen.getByLabelText('Value for credential') as HTMLInputElement).value).toBe('keep-default-draft');
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe('keep-this-draft');
});
