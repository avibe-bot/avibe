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
  sealBlindBox.mockReset().mockResolvedValue(box);
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

// Seeded transition walks use a record-identity oracle, independent of the editor's helpers.
// Observable contract: every settled plan has correct validity and every accepted value keeps
// its BARE/EXTRA identity. Directed cases do not cover arbitrary interleavings of drafts,
// Undo, external shape changes (even without a version change), Cancel and subsequent Saves.
it.each([12, 23, 47, 101, 211, 431, 863, 1723])('keeps final-layout invariants through seeded transitions (%s)', async (seed) => {
  type Store = { label: string; values: Map<string, string>; version: number };
  const extraId = (name: string) => `extra:${name}`;
  const fieldName = (id: string, store: Store) => id === 'bare' ? store.label : id.slice(6);
  const copyStore = (store: Store): Store => ({ ...store, values: new Map(store.values) });
  const initialCount = [1, 31, 32][seed % 3];
  let store: Store = { label: 'password', values: new Map([['bare', 'original']]), version: 1 };
  for (let i = 1; i < initialCount; i++) store.values.set(extraId(`f${i}`), 'original');
  let truth = copyStore(store);
  let drafts = new Map<string, string>();
  let label = truth.label;
  let labelEdited = false;
  let addition: { name: string; value: string } | null = null;
  let removals = new Set<string>();
  const payload = (value: Store): VaultSecret => ({ ...secret, default_field: value.label,
    fields: [...value.values.keys()].map((id) => fieldName(id, value)), updated_at: `v${value.version}` });
  const boxes = new Map<string, { value: string; record: string }>();
  sealBlindBox.mockImplementation(async (value?: unknown, _key?: unknown, context?: unknown) => {
    const ct = `box-${boxes.size}`;
    boxes.set(ct, { value: String(value), record: (context as { name: string }).name });
    return { ...box, ct };
  });
  // Independent backend effect ledger: validate/apply atomically, without reconciling UI drafts.
  api.updateVaultSecretFields.mockImplementation(async (_name, plan) => {
    const next = copyStore(store);
    const finalLabel = plan.default_field ?? store.label;
    if (plan.expected_updated_at !== `v${store.version}`) return { ok: false, code: 'secret_conflict' };
    if (finalLabel !== store.label && store.values.has(extraId(finalLabel))) throw Error('oracle_default_collision');
    const changed = new Set<string>();
    for (const value of plan.values ?? []) {
      if (changed.has(value.field) || plan.remove.includes(value.field)) throw Error('oracle_duplicate_change');
      changed.add(value.field);
      const id = value.field === finalLabel ? 'bare' : extraId(value.field);
      const expectedRecord = id === 'bare' ? 'LOGIN' : `LOGIN.${value.field}`;
      const sealed = boxes.get(value.blind_box.ct);
      if (!sealed || value.record !== expectedRecord || sealed.record !== expectedRecord) throw Error('oracle_record_mismatch');
      next.values.set(id, sealed.value);
    }
    for (const field of plan.remove ?? []) {
      if (field === finalLabel || field === store.label || !next.values.delete(extraId(field))) throw Error('oracle_invalid_removal');
    }
    if (next.values.size > 32) throw Error('oracle_capacity');
    next.label = finalLabel;
    if ((plan.values?.length ?? 0) + (plan.remove?.length ?? 0)) next.version++;
    store = next;
    return { ok: true, secret: payload(store) };
  });
  api.listVaultSecrets.mockImplementation(async () => ({ ok: true, secrets: [payload(store)] }));
  let view = renderEditor(payload(store));
  let randomState = seed;
  const pick = (n: number) => { randomState = (Math.imul(randomState, 1664525) + 1013904223) >>> 0; return Math.floor(randomState / 0x100000000 * n); };
  const names = ['password', 'username', 'credential', 'note', 'f1', 'f2', '', 'Bad-name'];
  const actions = ['edit-value', 'clear-value', 'rename-default', 'open-new', 'fill-new', 'remove-extra',
    'undo', 'discard-new', 'external-role-change', 'external-add-or-rename', 'save', 'cancel'];
  const trace: string[] = [];
  const state = () => {
    const extraNames = [...truth.values.keys()].filter((id) => id !== 'bare').map((id) => id.slice(6));
    const validName = (name: string) => /^[a-z][a-z0-9_]{0,63}$/.test(name);
    const meaningful = [...drafts].some(([id, value]) => value !== '' && !removals.has(id));
    const dirty = meaningful || label !== truth.label || removals.size > 0 || Boolean(addition && (addition.name || addition.value));
    const count = truth.values.size - removals.size + (addition ? 1 : 0);
    const valid = !dirty || (count <= 32 && validName(label) && !extraNames.includes(label)
      && (!addition || (validName(addition.name) && Boolean(addition.value) && addition.name !== label && !extraNames.includes(addition.name))));
    return { dirty, valid };
  };
  const reconcile = () => {
    truth = copyStore(store);
    for (const id of drafts.keys()) if (!truth.values.has(id)) drafts.delete(id);
    removals = new Set([...removals].filter((id) => id !== 'bare' && truth.values.has(id)));
    if (!labelEdited) label = truth.label;
  };
  const remove = (id: string) => {
    fireEvent.click(screen.getByRole('button', { name: `Remove ${fieldName(id, truth)}` }));
    fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
    removals.add(id);
  };
  for (let step = 0; step < 40; step++) {
    const action = pick(12);
    trace.push(actions[action]);
    const evidence = `seed=${seed}; prefix=${trace.join(',')}`;
    const before = api.updateVaultSecretFields.mock.calls.length;
    const beforeSeal = sealBlindBox.mock.calls.length;
    const visible = [...truth.values.keys()].filter((id) => !removals.has(id));
    if (action <= 1) {
      const id = visible[pick(visible.length)];
      const value = action === 0 ? `edit-${seed}-${step}` : '';
      change(`Value for ${fieldName(id, truth)}`, value); drafts.set(id, value);
    } else if (action === 2) {
      const nextLabel = names[pick(names.length)];
      // A same-value DOM change is not an edit event; edit intent begins on an actual change.
      if (nextLabel !== label) labelEdited = true;
      label = nextLabel; change('Default field name', label);
    } else if (action === 3 && !addition && visible.length < 32) {
      const button = screen.getByRole('button', { name: 'Add field' }) as HTMLButtonElement;
      expect(button.disabled, evidence).toBe(false); fireEvent.click(button); addition = { name: '', value: '' };
    } else if (action === 4 && addition) {
      addition.name = names[pick(names.length)]; addition.value = `new-${seed}-${step}`;
      change('New field name', addition.name); change('New field value', addition.value);
    } else if (action === 5 && visible.length > 1) {
      remove(visible.filter((id) => id !== 'bare')[pick(visible.length - 1)]);
    } else if (action === 6 && removals.size) {
      fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.undoRemove })); removals.clear();
    } else if (action === 7 && addition) {
      fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.discardNew })); addition = null;
    } else if (action === 8 || action === 9) {
      const extras = [...store.values.keys()].filter((id) => id !== 'bare');
      if (action === 8 && extras.length) {
        // Reachable coalesced truth: delete an extra, then rename the bare record to its label.
        const id = extras[pick(extras.length)]; store.values.delete(id); store.label = id.slice(6); store.version++;
      } else {
        const name = `external${step}`;
        if (pick(2) && store.values.size < 32) { store.values.set(extraId(name), 'external'); store.version++; }
        else store.label = name; // Label-only rename deliberately leaves the CAS timestamp unchanged.
      }
      reconcile();
      await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
    } else if (action === 10) {
      const expected = state();
      const accepted = copyStore(truth);
      if (expected.dirty && expected.valid) {
        for (const id of removals) accepted.values.delete(id);
        for (const [id, value] of drafts) if (value && !removals.has(id)) accepted.values.set(id, value);
        if (addition) accepted.values.set(extraId(addition.name), addition.value);
        accepted.label = label;
      }
      const result = await save();
      expect(result, evidence).toBe(expected.valid);
      if (expected.dirty && expected.valid) {
        expect(store.label === accepted.label && store.values.size === accepted.values.size
          && [...accepted.values].every(([id, value]) => store.values.get(id) === value), evidence).toBe(true);
        expect(store.values.size <= 32 && store.values.has('bare'), evidence).toBe(true);
        if (label !== truth.label) labelEdited = false;
        truth = copyStore(store); drafts.clear(); removals.clear(); addition = null; label = truth.label;
      } else {
        expect(api.updateVaultSecretFields.mock.calls.length, evidence).toBe(before);
        expect(sealBlindBox.mock.calls.length, evidence).toBe(beforeSeal);
      }
    } else if (action === 11) {
      view.unmount(); truth = copyStore(store); drafts = new Map(); removals = new Set(); addition = null; label = truth.label; labelEdited = false;
      view = renderEditor(payload(store));
    }
    expect(lastState(), evidence).toEqual(state());
    // Read the DOM once per transition instead of rescanning all labels for every field at
    // capacity. Keep the same per-identity checks, including unique accessible input labels.
    const inputs = new Map<string, string>();
    for (const input of view.container.querySelectorAll<HTMLInputElement>('input[aria-label]')) {
      const name = input.getAttribute('aria-label')!;
      if (!name.startsWith('Value for ')) continue;
      expect(inputs.has(name), evidence).toBe(false);
      inputs.set(name, input.value);
    }
    expect(inputs.size, evidence).toBe(truth.values.size - removals.size);
    for (const id of truth.values.keys()) {
      if (removals.has(id)) continue;
      expect(inputs.get(`Value for ${fieldName(id, truth)}`) === (drafts.get(id) ?? ''), evidence).toBe(true);
    }
    if (action !== 10) {
      expect(api.updateVaultSecretFields.mock.calls.length, evidence).toBe(before);
      expect(sealBlindBox.mock.calls.length, evidence).toBe(beforeSeal);
    }
  }
// Forty rendered transitions at up to 32 fields are a scenario, not a single interaction.
// Give shared CI workers a bounded budget without reducing seeds, steps or invariants.
}, 15_000);

// The name rule shows while a field name has focus and is hidden in place on blur. Unmounting it
// moved the dialog's Save up mid-click (focus leaves the name on the press), so the click was lost.
it('keeps the field-name rule in place, hidden and unannounced, when the name loses focus', () => {
  renderEditor();
  const name = screen.getByLabelText('Default field name');
  fireEvent.focus(name);
  const rule = screen.getByText(en.vaults.fields.nameRule);
  expect(rule.classList.contains('invisible')).toBe(false);
  expect(rule.getAttribute('aria-hidden')).toBeNull();
  fireEvent.blur(name);
  expect(screen.getByText(en.vaults.fields.nameRule)).toBe(rule);
  expect(rule.classList.contains('invisible')).toBe(true);
  expect(rule.getAttribute('aria-hidden')).toBe('true');
});

it('says nothing about an empty new field’s blank name when it loses focus; it is not saved', () => {
  renderEditor();
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  const name = screen.getByLabelText('New field name');
  fireEvent.blur(name);
  expect(screen.queryByText(en.vaults.fields.nameRequired)).toBeNull();
  expect(lastState()).toEqual({ dirty: false, valid: true });
  // Once it has a value, the blank name is what blocks Save, and it says so.
  change('New field value', 'seed');
  expect(screen.getByText(en.vaults.fields.nameRequired)).toBeTruthy();
  expect(lastState()).toEqual({ dirty: true, valid: false });
});

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

it('can undo a staged removal before saving, with the field’s draft intact', async () => {
  renderEditor();
  change('Value for username', 'kept-draft');
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect(screen.queryByLabelText('Value for username')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.undoRemove }));
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe('kept-draft');
  expect(await save()).toBe(true);
  expect(batch()[1]).toMatchObject({ values: [{ field: 'username', record: 'LOGIN.username', blind_box: box }], remove: [] });
});

it('lets a staged removal free a place for an addition at the field limit', async () => {
  const full = { ...secret, fields: ['password', ...Array.from({ length: 31 }, (_, i) => `f${i}`)] };
  renderEditor(full);
  expect((screen.getByRole('button', { name: 'Add field' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Remove f0' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect((screen.getByRole('button', { name: 'Add field' }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'replacement');
  change('New field value', 'seed');
  expect(await save()).toBe(true);
  expect(batch()[1]).toMatchObject({ values: [{ field: 'replacement' }], remove: ['f0'] });
});

it('does not send a staged-removed field’s draft', async () => {
  renderEditor();
  change('Value for username', 'dropped-draft');
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect(await save()).toBe(true);
  expect(batch()[1]).toMatchObject({ values: [], remove: ['username'] });
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

it('lets a new field take the renamed default’s old label', async () => {
  renderEditor();
  change('Default field name', 'credential');
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'password');
  change('New field value', 'seed');
  expect(lastState().valid).toBe(true);
  expect(await save()).toBe(true);
  expect(batch()[1]).toMatchObject({ default_field: 'credential', values: [{ field: 'password', record: 'LOGIN.password', blind_box: box }] });
});

it('drops a staged removal that another write already did', async () => {
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [{ ...secret, fields: ['password'], updated_at: 'version-2' }] });
  renderEditor();
  fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
  await waitFor(() => expect(screen.queryByText(/Removed when you save/)).toBeNull());
  expect(lastState().dirty).toBe(false);
  expect(await save()).toBe(true);
  expect(api.updateVaultSecretFields).not.toHaveBeenCalled();
});

// Drafts belong to records, not labels. A former extra must never become the bare default.
it.each([
  { staged: false, defaultDraft: '' },
  { staged: true, defaultDraft: '' },
  { staged: false, defaultDraft: 'real-default-draft' },
  { staged: true, defaultDraft: 'real-default-draft' },
])('drops a role-changed extra draft while preserving the actual default ($staged, $defaultDraft)', async ({ staged, defaultDraft }) => {
  const truth = { ...secret, default_field: 'username', fields: ['username', 'password'], updated_at: 'version-2' };
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [truth] });
  renderEditor();
  if (defaultDraft) change('Value for password', defaultDraft);
  change('Value for username', 'old-extra-draft');
  if (staged) {
    fireEvent.click(screen.getByRole('button', { name: 'Remove username' }));
    fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  }
  await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
  expect((screen.getByLabelText('Value for username') as HTMLInputElement).value).toBe(defaultDraft);
  expect((screen.getByLabelText('Value for password') as HTMLInputElement).value).toBe('');
  expect(screen.getByRole('status').textContent).toContain('username');
  expect(lastState()).toEqual({ dirty: Boolean(defaultDraft), valid: true });
  expect(await save()).toBe(true);
  if (defaultDraft) {
    expect(batch()[1]).toMatchObject({ values: [{ field: 'username', record: 'LOGIN', blind_box: box }], remove: [] });
    expect(sealBlindBox.mock.calls.map((call) => (call as unknown[])[0])).toEqual([defaultDraft]);
  } else {
    expect(sealBlindBox).not.toHaveBeenCalled();
    expect(api.updateVaultSecretFields).not.toHaveBeenCalled();
  }
});

// Admission at Add is not enough: Undo and live truth can both grow the final layout.
it.each(['undo', 'refetch'] as const)('blocks over-capacity drafts after %s before sealing and preserves a way to resolve them', async (transition) => {
  const full = { ...secret, fields: ['password', ...Array.from({ length: 31 }, (_, i) => `f${i}`)] };
  renderEditor(transition === 'undo' ? full : { ...full, fields: full.fields.slice(0, -1) });
  if (transition === 'undo') {
    fireEvent.click(screen.getByRole('button', { name: 'Remove f0' }));
    fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  }
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'replacement');
  change('New field value', 'kept-new-draft');
  if (transition === 'undo') {
    fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.undoRemove }));
  } else {
    api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [{ ...full, updated_at: 'version-2' }] });
    await act(async () => { eventHandlers?.onVaultsUpdated?.({ scope: 'secret', secret_name: 'LOGIN' }); });
  }
  expect(lastState()).toEqual({ dirty: true, valid: false });
  expect(screen.getByRole('alert').textContent).toBe(en.errors.too_many_fields);
  expect(await save()).toBe(false);
  expect(sealBlindBox).not.toHaveBeenCalled();
  expect(vault.sealValue).not.toHaveBeenCalled();
  expect(api.updateVaultSecretFields).not.toHaveBeenCalled();
  expect((screen.getByLabelText('New field value') as HTMLInputElement).value).toBe('kept-new-draft');
  fireEvent.click(screen.getByRole('button', { name: 'Remove f0' }));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.confirmRemove }));
  expect(lastState().valid).toBe(true);
  expect(screen.queryByRole('alert')).toBeNull();
  expect(await save()).toBe(true);
  expect(batch()[1]).toMatchObject({ values: [{ field: 'replacement' }], remove: ['f0'] });
});

it('revalidates capacity before resealing on a record-mismatch retry', async () => {
  const full = { ...secret, fields: ['password', ...Array.from({ length: 31 }, (_, i) => `f${i}`)] };
  renderEditor({ ...full, fields: full.fields.slice(0, -1) });
  fireEvent.click(screen.getByRole('button', { name: 'Add field' }));
  change('New field name', 'replacement');
  change('New field value', 'new-draft');
  api.updateVaultSecretFields.mockResolvedValueOnce({ ok: false, code: 'record_mismatch' });
  api.listVaultSecrets.mockResolvedValueOnce({ ok: true, secrets: [{ ...full, updated_at: 'version-2' }] });
  expect(await save()).toBe(false);
  expect(sealBlindBox).toHaveBeenCalledOnce();
  expect(api.updateVaultSecretFields).toHaveBeenCalledOnce();
  expect(lastState().valid).toBe(false);
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
  // Nothing drafted: the dialog may still save its metadata.
  expect(lastState()).toEqual({ dirty: false, valid: true });
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
