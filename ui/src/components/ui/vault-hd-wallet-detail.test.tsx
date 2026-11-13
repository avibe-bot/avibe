/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import en from '@/i18n/en.json';
import type { VaultHdAccount, VaultSecret } from '@/context/ApiContext';
import { VaultHdWalletDialog } from './vault-hd-wallet-detail';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

const api = vi.hoisted(() => ({
  enableVaultHdAccount: vi.fn(),
  updateVaultHdAccount: vi.fn(),
  prepareVaultBitcoinAddressTypes: vi.fn(async () => ({ ok: false, code: 'forbidden' })),
  getSession: vi.fn(async () => ({ title: '网格交易' })),
}));
vi.mock('@/context/ApiContext', async (original) => ({ ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api }));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const w = en.vaults.hd.wallet;
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const account = (n: number, extra: Partial<VaultHdAccount> = {}): VaultHdAccount => ({
  account: n, name: null, eth: `0xETH${n}`, btc_taproot: `bc1pBTC${n}`, solana: `SOL${n}`,
  paths: { evm: `m/44'/60'/0'/0/${n}`, bitcoin: `m/86'/0'/0'/0/${n}`, solana: `m/44'/501'/${n}'/0'` },
  always_ask: false, last_signer: null, ...extra,
});
const wallet = (accounts: VaultHdAccount[]): VaultSecret => ({
  name: 'AGENT_WALLET', tags: [], kind: 'keypair', protection: 'standard', signer_kind: 'hd-seed', source: 'user', policy: {},
  last_used_at: null, use_count: 0, created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z', accounts, account_limit: 20,
});
const open = (secret: VaultSecret, onChanged = vi.fn(), canManage = true) => {
  const capabilities = { ...OWNER_INSTANCE_CAPABILITIES, can_manage_instance: canManage };
  render(
    <I18nextProvider i18n={i18n}>
      <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: null, capabilities }}>
        <VaultHdWalletDialog secret={secret} open onOpenChange={vi.fn()} onChanged={onChanged} />
      </InstanceAuthorizationContext.Provider>
    </I18nextProvider>,
  );
  return onChanged;
};

it('shows each account as the server lists it: index, name, addresses and the Session that last signed', async () => {
  open(wallet([account(0), account(1, { name: 'trading', last_signer: { session_id: 'sesabc', at: new Date().toISOString() } })]));
  expect(screen.getByText(en.vaults.hd.mainAccount)).toBeTruthy();
  expect(screen.getByText('trading')).toBeTruthy();
  for (const text of ['0xETH0', 'bc1pBTC0', 'SOL0', '0xETH1', 'bc1pBTC1', 'SOL1']) expect(screen.getByText(text)).toBeTruthy();
  expect(await screen.findByText(/网格交易/)).toBeTruthy();
  expect(screen.getByText(w.used.replace('{{count}}', '2').replace('{{limit}}', '20'))).toBeTruthy();
});

it('adds the next account without asking for the words', async () => {
  const next = wallet([account(0), account(1)]);
  api.enableVaultHdAccount.mockResolvedValue({ ok: true, secret: next });
  const onChanged = open(wallet([account(0)]));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: w.add.replace('{{account}}', '1') })); });
  expect(api.enableVaultHdAccount).toHaveBeenCalledWith('AGENT_WALLET', { handleError: false });
  expect(onChanged).toHaveBeenCalledWith(next);
});

it('turns ask-before-signing on, and does not let it be turned off without the Passkey check', async () => {
  api.updateVaultHdAccount.mockResolvedValue({ ok: true, secret: wallet([account(0, { always_ask: true })]) });
  open(wallet([account(0)]));
  await act(async () => { fireEvent.click(screen.getByRole('switch', { name: w.ask })); });
  expect(api.updateVaultHdAccount).toHaveBeenCalledWith('AGENT_WALLET', 0, { always_ask: true }, { handleError: false });
  cleanup();
  api.updateVaultHdAccount.mockClear();
  open(wallet([account(0, { always_ask: true })]));
  const toggle = screen.getByRole('switch', { name: w.ask }) as HTMLButtonElement;
  expect(toggle.disabled).toBe(true);
  fireEvent.click(toggle);
  expect(api.updateVaultHdAccount).not.toHaveBeenCalled();
  expect(screen.getByLabelText(w.askLocked)).toBeTruthy();
});

it('renames an account, and stops offering more at the limit', async () => {
  api.updateVaultHdAccount.mockResolvedValue({ ok: true, secret: wallet([account(0, { name: 'main' })]) });
  open(wallet([account(0)]));
  fireEvent.click(screen.getByRole('button', { name: w.rename }));
  const input = screen.getByPlaceholderText(w.namePlaceholder);
  fireEvent.change(input, { target: { value: 'treasury' } });
  await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }); });
  expect(api.updateVaultHdAccount).toHaveBeenCalledWith('AGENT_WALLET', 0, { name: 'treasury' }, { handleError: false });
  cleanup();
  open(wallet(Array.from({ length: 20 }, (_, i) => account(i))));
  expect(screen.queryByRole('button', { name: /Add account/ })).toBeNull();
  expect(screen.getByText(w.full.replace('{{limit}}', '20'))).toBeTruthy();
  expect(within(document.body).getAllByText(/^#\d+$/)).toHaveLength(20);
});

it('says so when a connection drops while adding an account or turning ask on', async () => {
  api.enableVaultHdAccount.mockRejectedValueOnce(new TypeError('Failed to fetch'));
  api.updateVaultHdAccount.mockRejectedValueOnce(new TypeError('Failed to fetch'));
  open(wallet([account(0)]));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: w.add.replace('{{account}}', '1') })); });
  expect(screen.getAllByRole('alert').length).toBeGreaterThan(0);
  await act(async () => { fireEvent.click(screen.getByRole('switch', { name: w.ask })); });
  expect(screen.getAllByRole('alert').length).toBe(2);
});

it('shows an Editor the addresses but no account-management actions, and says why', () => {
  open(wallet([account(0), account(1)]), vi.fn(), false);
  expect(screen.getAllByText(account(0).eth).length).toBeGreaterThan(0);
  expect(screen.queryByRole('button', { name: en.vaults.hd.wallet.rename })).toBeNull();
  expect(screen.queryByRole('button', { name: /\+|Add account/ })).toBeNull();
  for (const sw of screen.getAllByRole('switch', { name: en.vaults.hd.wallet.ask })) expect((sw as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(en.vaults.hd.wallet.needsManager)).toBeTruthy();
});

it('applies a preparation answer even when the page rerenders with a new handler meanwhile', async () => {
  let resolve!: (value: unknown) => void;
  api.prepareVaultBitcoinAddressTypes.mockReturnValueOnce(new Promise((r) => { resolve = r; }) as never);
  const first = vi.fn();
  const second = vi.fn();
  const unprepared = wallet([account(0)]);
  const view = (onChanged: (s: VaultSecret) => void) => (
    <I18nextProvider i18n={i18n}>
      <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: null, capabilities: OWNER_INSTANCE_CAPABILITIES }}>
        <VaultHdWalletDialog secret={unprepared} open onOpenChange={vi.fn()} onChanged={onChanged} />
      </InstanceAuthorizationContext.Provider>
    </I18nextProvider>
  );
  const { rerender } = render(view(first));
  rerender(view(second));
  const prepared = { ...unprepared, bitcoin_profile: 'bitcoin-address-types-v1' as const };
  await act(async () => { resolve({ ok: true, secret: prepared }); });
  expect(api.prepareVaultBitcoinAddressTypes).toHaveBeenCalledOnce();
  expect(second).toHaveBeenCalledWith(prepared);
});
