/* @vitest-environment jsdom */

// The generator attached to the value field (avibe#3338; ui/design: vault/generate-secret): a click
// fills the field, every setting change regenerates it, closing keeps it, typing makes it manual, and
// Save stores exactly what the field shows — sealed only then, on both tiers.
import { webcrypto } from 'node:crypto';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import type { ReactElement } from 'react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import type { VaultRequestSpec } from '@/context/ApiContext';
import { VaultSecretForm } from './vault-secret-form';

const api = vi.hoisted(() => ({
  listDependencies: vi.fn(),
  listVaultSecrets: vi.fn(async () => ({ secrets: [] })),
  listSkills: vi.fn(async () => ({ skills: [] })),
  createVaultSecret: vi.fn(),
  updateVaultSecret: vi.fn(async () => ({ ok: true })),
  getVaultPubkey: vi.fn(async () => ({ public_key: 'public', fingerprint: 'fingerprint' })),
  connectWorkbenchEvents: vi.fn(() => vi.fn()),
}));
const blindBox = vi.hoisted(() => ({ scheme: 'test', enc: 'enc', ct: 'ciphertext' }));
const sealBlindBox = vi.hoisted(() => vi.fn(async () => blindBox));
const envelope = vi.hoisted(() => ({ ciphertext: 'ciphertext', nonce: 'nonce', wrap_meta: '{}' }));
const vault = vi.hoisted(() => ({ status: 'unlocked', refresh: vi.fn(), afterCreated: vi.fn(), sealValue: vi.fn() }));
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
const valueInput = () => screen.getByLabelText(en.vaults.dialog.value) as HTMLInputElement;
const openGenerator = (name = en.vaults.generate.password) => fireEvent.click(screen.getByRole('button', { name }));
const submit = () => screen.getByRole('button', { name: en.vaults.request.saveAndWake }) as HTMLButtonElement;

beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  api.listDependencies.mockResolvedValue({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.12' }] });
  api.createVaultSecret.mockResolvedValue({ ok: true });
  vault.sealValue.mockImplementation(async (_name: string, _kind: string, value: { valueRef: { current: string }; clear: () => void }) => {
    value.clear();
    return { envelope, establishingVmk: false };
  });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it('fills the field at once, in clear while open, and regenerates on every change', async () => {
  render(form(<VaultSecretForm fixedName="DB_PASSWORD" onCancel={vi.fn()} onCreated={vi.fn()} />));
  openGenerator();
  const first = valueInput().value;
  expect(first).toHaveLength(24);
  expect(valueInput().type).toBe('text');

  fireEvent.change(screen.getByRole('slider', { name: en.vaults.generate.length }), { target: { value: '40' } });
  expect(valueInput().value).toHaveLength(40);

  fireEvent.click(screen.getByRole('switch', { name: en.vaults.generate.symbols }));
  expect(valueInput().value).toMatch(/^[A-Za-z0-9]{40}$/);

  fireEvent.click(screen.getByRole('radio', { name: en.vaults.generate.typeHex }));
  expect(valueInput().value).toMatch(/^[0-9a-f]{64}$/);

  const before = valueInput().value;
  fireEvent.click(screen.getByRole('button', { name: en.vaults.generate.regenerate }));
  expect(valueInput().value).not.toBe(before);
  // The popup is modal, so the field behind it is hidden from the accessibility tree meanwhile.
  expect(screen.getByRole('button', { name: en.vaults.generate.key, hidden: true })).toBeTruthy();
});

it('keeps the value when the popup closes, masked and marked, and typing makes it manual', async () => {
  render(form(<VaultSecretForm fixedName="DB_PASSWORD" onCancel={vi.fn()} onCreated={vi.fn()} />));
  openGenerator();
  const generated = valueInput().value;
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('slider')).toBeNull());
  expect(valueInput().value).toBe(generated);
  expect(valueInput().type).toBe('password');
  expect(screen.getByText(en.vaults.generate.generated)).toBeTruthy();
  // Pressing the control again leaves focus put: Safari doesn't focus a pressed button, so focus
  // left the field, the control's name hid and it shrank out from under the press.
  expect(fireEvent.mouseDown(screen.getByRole('button', { name: en.vaults.generate.password }))).toBe(false);

  fireEvent.change(valueInput(), { target: { value: `${generated}x` } });
  expect(screen.queryByText(en.vaults.generate.generated)).toBeNull();
});

it('keeps the size box exact: out-of-range typing is not applied and snaps back', () => {
  render(form(<VaultSecretForm fixedName="DB_PASSWORD" onCancel={vi.fn()} onCreated={vi.fn()} />));
  openGenerator();
  const box = screen.getByRole('textbox', { name: en.vaults.generate.length }) as HTMLInputElement;
  fireEvent.change(box, { target: { value: '7' } });
  expect(box.getAttribute('aria-invalid')).toBe('true');
  expect(valueInput().value).toHaveLength(24);
  fireEvent.blur(box);
  expect(box.value).toBe('24');
  fireEvent.change(box, { target: { value: '32' } });
  expect(valueInput().value).toHaveLength(32);
});

it.each(['standard', 'protected'] as const)('saves exactly the value the field shows, sealed only at Save (%s)', async (protection) => {
  const onCreated = vi.fn();
  render(form(<VaultSecretForm fixedName="DB_PASSWORD" defaultProtection={protection} onCancel={vi.fn()} onCreated={onCreated} />));
  openGenerator();
  fireEvent.click(screen.getByRole('button', { name: en.vaults.generate.regenerate }));
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  const shown = valueInput().value;
  // Nothing is sealed or cached while generating: a lock or a new setup before Save can't leave an
  // envelope made under an old key behind.
  expect(vault.sealValue).not.toHaveBeenCalled();
  expect(sealBlindBox).not.toHaveBeenCalled();
  await waitFor(() => expect(submit().disabled).toBe(false));
  await act(async () => { fireEvent.click(submit()); });
  await waitFor(() => expect(api.createVaultSecret).toHaveBeenCalledOnce());
  if (protection === 'protected') {
    expect(vault.sealValue).toHaveBeenCalledOnce();
    // The sandbox got the field's value, and the page's copy was cleared.
    const [, , value] = vault.sealValue.mock.calls[0] as [string, string, { valueRef: { current: string } }];
    expect(value.valueRef.current).toBe('');
    expect(sealedValues).toContain(shown);
  } else {
    expect(sealBlindBox).toHaveBeenCalledWith(shown, expect.anything(), expect.anything());
  }
  expect(JSON.stringify(api.createVaultSecret.mock.calls)).not.toContain(shown);
});

const sealedValues: string[] = [];
beforeEach(() => {
  sealedValues.length = 0;
  vault.sealValue.mockImplementation(async (_name: string, _kind: string, value: { valueRef: { current: string }; clear: () => void }) => {
    sealedValues.push(value.valueRef.current);
    value.clear();
    return { envelope, establishingVmk: false };
  });
});

it('starts from the Agent’s suggestion, which only presets the options', () => {
  const spec = { generate: { type: 'hex', bytes: 32 } } as unknown as VaultRequestSpec;
  render(form(<VaultSecretForm fixedName="SESSION_SECRET" requestSpec={spec} onCancel={vi.fn()} onCreated={vi.fn()} />));
  expect(valueInput().value).toBe('');
  openGenerator(en.vaults.generate.key);
  expect(valueInput().value).toMatch(/^[0-9a-f]{64}$/);
  expect(screen.getByText(en.vaults.generate.suggested)).toBeTruthy();
  expect(api.createVaultSecret).not.toHaveBeenCalled();
});

it('keeps an older request’s 128-character preset as a 100-character one and says so', () => {
  const spec = { generate: { type: 'password', length: 128, symbols: false } } as unknown as VaultRequestSpec;
  const props = { fixedName: 'DB_PASSWORD', provisionRequestId: 'request', onCancel: vi.fn(), onCreated: vi.fn() };
  const { rerender } = render(form(<VaultSecretForm {...props} requestSpec={spec} />));
  // The pending-request list refreshes: the same request arrives as a new object.
  rerender(form(<VaultSecretForm {...props} requestSpec={{ ...spec }} />));
  openGenerator();
  expect(valueInput().value).toMatch(/^[A-Za-z0-9]{100}$/);
  expect(screen.getByText(i18n.t('vaults.generate.suggestedAdjusted', { min: 8, max: 100 }))).toBeTruthy();
  // Once the user picks a length, the note about the Agent's length no longer applies.
  fireEvent.change(screen.getByRole('slider', { name: en.vaults.generate.length }), { target: { value: '40' } });
  expect(screen.queryByText(i18n.t('vaults.generate.suggestedAdjusted', { min: 8, max: 100 }))).toBeNull();
  expect(screen.getByText(en.vaults.generate.suggested)).toBeTruthy();
});

it('generates one field row at a time and carries a generated single value into the default row', () => {
  render(form(<VaultSecretForm fixedName="APP_ADMIN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  openGenerator();
  const carried = valueInput().value;
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.add }));
  expect((screen.getByLabelText('Value for value') as HTMLInputElement).value).toBe(carried);
  expect(screen.getByText(en.vaults.generate.generated)).toBeTruthy();
  const rowButtons = screen.getAllByRole('button', { name: en.vaults.generate.password });
  fireEvent.click(rowButtons[rowButtons.length - 1]);
  const second = screen.getByLabelText('Value for 2') as HTMLInputElement;
  expect(second.value).toHaveLength(24);
  expect(second.type).toBe('text');
  expect((screen.getByLabelText('Value for value') as HTMLInputElement).value).toBe(carried);
});

it.each(['en', 'zh'] as const)('names the control in %s without English leaking into Chinese', async (language) => {
  await i18n.changeLanguage(language);
  const copy = language === 'zh' ? zh : en;
  render(form(<VaultSecretForm fixedName="DB_PASSWORD" onCancel={vi.fn()} onCreated={vi.fn()} />));
  fireEvent.click(screen.getByRole('button', { name: copy.vaults.generate.password }));
  expect(screen.getByText(copy.vaults.generate.digits)).toBeTruthy();
  if (language === 'zh') expect(screen.queryByText(/characters|Regenerate/)).toBeNull();
  await i18n.changeLanguage('en');
});

it('keeps a field row’s new settings when the generator fills it (Hex stays Hex on Regenerate)', () => {
  render(form(<VaultSecretForm fixedName="APP_ADMIN" onCancel={vi.fn()} onCreated={vi.fn()} />));
  fireEvent.click(screen.getByRole('button', { name: en.vaults.fields.add }));
  const rowButtons = screen.getAllByRole('button', { name: en.vaults.generate.password });
  fireEvent.click(rowButtons[rowButtons.length - 1]);
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.generate.typeHex }));
  const row = () => screen.getByLabelText('Value for 2') as HTMLInputElement;
  expect(row().value).toMatch(/^[0-9a-f]{64}$/);
  expect((screen.getByRole('radio', { name: en.vaults.generate.typeHex }) as HTMLElement).getAttribute('aria-checked')).toBe('true');
  fireEvent.click(screen.getByRole('button', { name: en.vaults.generate.regenerate }));
  expect(row().value).toMatch(/^[0-9a-f]{64}$/);
});

it('lets the length be typed digit by digit in the same box', () => {
  render(form(<VaultSecretForm fixedName="DB_PASSWORD" onCancel={vi.fn()} onCreated={vi.fn()} />));
  openGenerator();
  const box = screen.getByRole('textbox', { name: en.vaults.generate.length }) as HTMLInputElement;
  box.focus();
  for (const text of ['', '1', '10', '100']) fireEvent.change(box, { target: { value: text } });
  expect(screen.getByRole('textbox', { name: en.vaults.generate.length })).toBe(box);
  expect(box.value).toBe('100');
  expect(valueInput().value).toHaveLength(100);
  // Passing through 10 on the way applied it; 101 is past the end and is not applied.
  fireEvent.change(box, { target: { value: '101' } });
  expect(box.getAttribute('aria-invalid')).toBe('true');
  expect(valueInput().value).toHaveLength(100);
});

it('keeps no copy of a protected generated value once sealing clears the field, even when saving fails', async () => {
  vault.sealValue.mockImplementation(async (_name: string, _kind: string, value: { clear: () => void }) => {
    value.clear();
    throw Object.assign(new Error('boom'), { code: 'sandbox_reset' });
  });
  render(form(<VaultSecretForm fixedName="DB_PASSWORD" defaultProtection="protected" onCancel={vi.fn()} onCreated={vi.fn()} />));
  openGenerator();
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  await waitFor(() => expect(submit().disabled).toBe(false));
  await act(async () => { fireEvent.click(submit()); });
  await waitFor(() => expect(vault.sealValue).toHaveBeenCalled());
  expect(valueInput().value).toBe('');
  expect(screen.queryByText(en.vaults.generate.generated)).toBeNull();
});
