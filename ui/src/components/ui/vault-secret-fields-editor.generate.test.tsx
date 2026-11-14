// @vitest-environment jsdom
// The generator on the edit dialog's field rows (avibe#3338 W2b; ui/design: vault/generate-secret) and
// the saved Agent/CLI provenance. A generated value is an ordinary draft with a flag: it goes with the
// one Save, follows the draft through refetches, and is cleared with it.
import { webcrypto } from 'node:crypto';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { VaultGeneratedField, VaultSecret } from '@/context/ApiContext';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { VaultSecretFieldsEditor, type VaultFieldsCommit } from './vault-secret-fields-editor';
import { VaultGeneratedOriginBadge } from './vault-generated-origin';

const api = vi.hoisted(() => ({
  updateVaultSecret: vi.fn(), updateVaultSecretFields: vi.fn(),
  listVaultSecrets: vi.fn(),
  connectWorkbenchEvents: vi.fn(() => vi.fn()),
  getVaultPubkey: vi.fn(async () => ({ public_key: 'public' })),
  getSession: vi.fn(),
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
let eventHandlers: { onVaultsUpdated?: (data: { scope: string; secret_name?: string }) => void } | null = null;
const commitRef: { current: VaultFieldsCommit | null } = { current: null };
const editor = (entry: VaultSecret) => (
  <MemoryRouter>
    <I18nextProvider i18n={i18n}>
      <VaultSecretFieldsEditor secret={entry} disabled={false} onBusyChange={vi.fn()} commitRef={commitRef} />
    </I18nextProvider>
  </MemoryRouter>
);
const renderEditor = (entry = secret) => render(editor(entry));
const save = async () => {
  let result: boolean | null = null;
  await act(async () => { result = await commitRef.current!.commit(); });
  return result;
};
const valueOf = (label: string) => (screen.getByLabelText(label) as HTMLInputElement).value;
// The row generators in row order; the popup is modal, so only reach them while it is closed.
const rowGenerator = (index: number) => screen.getAllByRole('button', { name: en.vaults.generate.password })[index];
const closePopup = async () => {
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('slider')).toBeNull());
};
const batch = () => api.updateVaultSecretFields.mock.calls[0];

beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  sealBlindBox.mockReset().mockResolvedValue(box);
  api.connectWorkbenchEvents.mockImplementation((handlers) => {
    eventHandlers = handlers;
    return vi.fn();
  });
  api.updateVaultSecretFields.mockResolvedValue({ ok: true, secret: { ...secret, updated_at: 'version-2' } });
  api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: [secret] });
  api.getSession.mockRejectedValue(new Error('not found'));
});
afterEach(async () => { cleanup(); eventHandlers = null; vi.clearAllMocks(); vi.unstubAllGlobals(); await i18n.changeLanguage('en'); });

it('fills a saved field’s draft in clear, regenerates on every change, and keeps it masked and marked on close', async () => {
  renderEditor();
  fireEvent.click(rowGenerator(0));
  const field = screen.getByLabelText('Value for password') as HTMLInputElement;
  expect(field.value).toHaveLength(24);
  expect(field.type).toBe('text');
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.generate.typeHex }));
  expect(field.value).toMatch(/^[0-9a-f]{64}$/);
  const before = field.value;
  fireEvent.click(screen.getByRole('button', { name: en.vaults.generate.regenerate }));
  expect(field.value).toMatch(/^[0-9a-f]{64}$/);
  expect(field.value).not.toBe(before);
  await closePopup();
  expect(field.type).toBe('password');
  expect(screen.getByText(en.vaults.generate.generated)).toBeTruthy();
  // Its settings stay with the row: reopening regenerates Hex, not the default password.
  fireEvent.click(screen.getByRole('button', { name: en.vaults.generate.key }));
  expect(field.value).toMatch(/^[0-9a-f]{64}$/);
  await closePopup();
  // Typing over it makes it the user's own.
  fireEvent.change(field, { target: { value: 'typed' } });
  expect(screen.queryByText(en.vaults.generate.generated)).toBeNull();
});

it('generates the new field’s value too, and saves every draft in the one atomic request', async () => {
  const after = { ...secret, fields: ['password', 'username', 'api_token'], updated_at: 'version-2' };
  api.updateVaultSecretFields.mockResolvedValueOnce({ ok: true, secret: after });
  renderEditor();
  fireEvent.click(rowGenerator(1));
  const username = valueOf('Value for username');
  await closePopup();
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  fireEvent.change(screen.getByLabelText('New field name'), { target: { value: 'api_token' } });
  fireEvent.click(rowGenerator(2));
  const added = valueOf('New field value');
  expect(added).toHaveLength(24);
  await closePopup();
  expect(screen.getAllByText(en.vaults.generate.generated)).toHaveLength(2);
  expect(await save()).toBe(true);
  expect(api.updateVaultSecretFields).toHaveBeenCalledOnce();
  expect(batch()[1]).toEqual({
    expected_updated_at: 'version-1',
    values: [
      { field: 'username', record: 'LOGIN.username', blind_box: box },
      { field: 'api_token', record: 'LOGIN.api_token', blind_box: box },
    ],
    remove: [],
  });
  expect(sealBlindBox.mock.calls.map((call) => (call as unknown[])[0])).toEqual([username, added]);
  expect(JSON.stringify(batch())).not.toContain(username);
  // Saved: the drafts and their marks are gone; saved truth comes from the server from here on.
  await waitFor(() => expect(valueOf('Value for username')).toBe(''));
  expect(screen.queryByText(en.vaults.generate.generated)).toBeNull();
});

it('keeps a generated draft through a staged removal and Undo, and never sends a removed one', async () => {
  renderEditor();
  fireEvent.click(rowGenerator(1));
  const generated = valueOf('Value for username');
  await closePopup();
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.undoRemove }));
  expect(valueOf('Value for username')).toBe(generated);
  expect(screen.getByText(en.vaults.generate.generated)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect(await save()).toBe(true);
  expect(batch()[1]).toMatchObject({ values: [], remove: ['username'] });
  expect(sealBlindBox).not.toHaveBeenCalled();
});

it('seals a protected generated value only at Save, and clears the page copy and its mark', async () => {
  const envelope = { ciphertext: 'ciphertext', nonce: 'nonce', wrap_meta: '{}' };
  const sealed: string[] = [];
  vault.sealValue.mockImplementation(async (_name, _kind, value) => {
    sealed.push(value.valueRef.current);
    value.clear();
    return { envelope };
  });
  renderEditor({ ...secret, protection: 'protected' });
  fireEvent.click(rowGenerator(0));
  const shown = valueOf('Value for password');
  await closePopup();
  expect(vault.sealValue).not.toHaveBeenCalled();
  expect(await save()).toBe(true);
  expect(sealed).toEqual([shown]);
  expect(batch()[1].values).toEqual([{ field: 'password', record: 'LOGIN', sealed: envelope }]);
  expect(valueOf('Value for password')).toBe('');
  expect(screen.queryByText(en.vaults.generate.generated)).toBeNull();
});

it('moves a generated draft and its mark with the default field when another write renames it', async () => {
  const { rerender } = renderEditor();
  fireEvent.click(rowGenerator(0));
  const generated = valueOf('Value for password');
  await closePopup();
  const renamed = { ...secret, default_field: 'credential', fields: ['credential', 'username'], updated_at: 'version-2' };
  api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: [renamed] });
  rerender(editor(renamed));
  await waitFor(() => expect(valueOf('Value for credential')).toBe(generated));
  expect(screen.getByText(en.vaults.generate.generated)).toBeTruthy();
});

it('keeps an open generator on its record when another write gives its old label to a new field', async () => {
  const { rerender } = renderEditor();
  fireEvent.click(rowGenerator(0));
  const generated = valueOf('Value for password');
  // Another writer renamed the default to credential and added a new extra field called password.
  const moved = { ...secret, default_field: 'credential', fields: ['credential', 'username', 'password'], updated_at: 'version-2' };
  api.listVaultSecrets.mockResolvedValue({ ok: true, secrets: [moved] });
  rerender(editor(moved));
  await waitFor(() => expect(valueOf('Value for credential')).toBe(generated));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.generate.regenerate }));
  expect(valueOf('Value for credential')).toHaveLength(24);
  expect(valueOf('Value for credential')).not.toBe(generated);
  expect(valueOf('Value for password')).toBe('');
});

it('drops a generated draft and its mark when another write removes the field', async () => {
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [{ ...secret, fields: ['password'], updated_at: 'version-2' }] });
  renderEditor();
  fireEvent.click(rowGenerator(1));
  await closePopup();
  await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
  await waitFor(() => expect(screen.queryByLabelText('Value for username')).toBeNull());
  expect(screen.queryByText(en.vaults.generate.generated)).toBeNull();
});

const provenance = (entry: Partial<VaultGeneratedField>): VaultGeneratedField => ({
  field: 'password', by: 'agent', session_id: 'ses_origin', session_title: 'Deploy web', at: new Date().toISOString(),
  type: 'hex', bytes: 32, ...entry,
});

it('says per field who generated a saved value, linking only a Session that still exists', async () => {
  api.getSession.mockImplementation(async (id: string) => ({ id, title: 'Deploy web service' }));
  renderEditor({ ...secret, generated: [
    provenance({}),
    provenance({ field: 'username', by: 'cli', session_id: null, session_title: null, type: 'password', length: 20, bytes: undefined }),
  ] });
  // The Session's title now, and a link to it.
  const link = await screen.findByRole('link', { name: /Open session/ });
  expect(link.getAttribute('href')).toBe('/chat/ses_origin');
  expect(screen.getByText(/Generated by an Agent in “Deploy web service”/).textContent).toContain('Hex, 32 bytes');
  expect(screen.getByText(/Generated from the command line/).textContent).toContain('20-character password');
  expect(screen.getAllByRole('link')).toHaveLength(1);
  expect(screen.getByText(en.vaults.generatedBy.agentNote)).toBeTruthy();
});

it.each([
  ['a deleted Session keeps the title it had, without a link', { session_title: 'Old title' }, /Generated by an Agent in “Old title”/],
  ['an untitled deleted Session says only “an Agent”', { session_title: null }, /^Generated by an Agent ·/],
])('%s', async (_, entry, text) => {
  renderEditor({ ...secret, generated: [provenance(entry)] });
  await waitFor(() => expect(api.getSession).toHaveBeenCalledWith('ses_origin', { handleError: false }));
  expect(await screen.findByText(text)).toBeTruthy();
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.queryByText(/ses_origin/)).toBeNull();
});

it.each([
  ['one field: the Session it was made in', { ...secret, fields: ['password'], generated: [provenance({})] }, 'Generated by an Agent · Deploy web'],
  ['several fields: which ones', { ...secret, generated: [provenance({}), provenance({ field: 'username' })] }, 'Generated by an Agent · password, username'],
  ['the CLI', { ...secret, fields: ['password'], generated: [provenance({ by: 'cli', session_id: null, session_title: null })] }, 'Generated from the CLI'],
  ['nothing generated', secret, null],
])('marks a generated secret in the Vaults list (%s)', (_, entry, text) => {
  const { container } = render(<I18nextProvider i18n={i18n}><VaultGeneratedOriginBadge secret={entry} /></I18nextProvider>);
  expect(container.textContent || null).toBe(text);
});
