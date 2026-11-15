/* @vitest-environment jsdom */

// The switch that holds the Protected tier for new seed-phrase wallets (lib/vaultProtectedSeedSwitch).
// Off, as shipped: a seed wallet is offered on Standard only, with nothing promising Protected; other
// keys keep both tiers. On: the Protected seed wallet is offered again.
import type { ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';

import en from '@/i18n/en.json';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import { PROTECTED_SEED_WALLET_ENABLED } from '@/lib/vaultProtectedSeedSwitch';

import { VaultSecretForm } from './vault-secret-form';

const api = vi.hoisted(() => ({
  listDependencies: vi.fn(async () => ({ ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.12' }] })),
  listVaultSecrets: vi.fn(async () => ({ ok: true, secrets: [] })),
  listSkills: vi.fn(async () => ({ skills: [] })),
  createVaultSecret: vi.fn(),
  getVaultPubkey: vi.fn(),
  connectWorkbenchEvents: vi.fn(() => vi.fn()),
}));
const vault = vi.hoisted(() => ({ status: 'locked', refresh: vi.fn(), afterCreated: vi.fn(), sealValue: vi.fn(), sealHdWallet: vi.fn() }));
const seedSwitch = vi.hoisted(() => ({ PROTECTED_SEED_WALLET_ENABLED: false }));
vi.mock('@/context/ApiContext', async (original) => ({ ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api }));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => vault, useProtectedHdSupported: () => true, useVaultSandboxWarm: () => true }));
vi.mock('./vault-protected-unlock', () => ({ VaultProtectedUnlock: () => <div role="note">protected-unlock</div> }));
vi.mock('@/lib/vaultProtectedSeedSwitch', () => ({
  get PROTECTED_SEED_WALLET_ENABLED() { return seedSwitch.PROTECTED_SEED_WALLET_ENABLED; },
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const d = en.vaults.dialog;
const hd = en.vaults.hd;
const AsOwner = ({ children }: { children: ReactNode }) => (
  <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: null, capabilities: OWNER_INSTANCE_CAPABILITIES }}>{children}</InstanceAuthorizationContext.Provider>
);
const protectedCard = () => screen.queryByRole('button', { name: new RegExp(`^${d.protectedProtection}`) });
const standardCard = () => screen.getByRole('button', { name: new RegExp(`^${d.standardProtection}`) });
const open = async () => {
  render(<I18nextProvider i18n={i18n}><AsOwner><VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} /></AsOwner></I18nextProvider>);
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: d.kindKeypair }));
};

beforeEach(() => {
  vi.stubGlobal('isSecureContext', true);
  sessionStorage.clear();
  seedSwitch.PROTECTED_SEED_WALLET_ENABLED = false;
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it('ships off: turning the Protected seed wallet on is this one constant', () => {
  expect(PROTECTED_SEED_WALLET_ENABLED).toBe(false);
});

it('off: a seed wallet is offered on Standard only, and nothing on the page promises Protected for it', async () => {
  await open();
  // Another key keeps both tiers; Protected chosen there…
  fireEvent.click(protectedCard()!);
  expect(protectedCard()!.getAttribute('aria-pressed')).toBe('true');
  // …a seed wallet falls back to Standard, with no Protected card to pick.
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  expect(protectedCard()).toBeNull();
  expect(standardCard().getAttribute('aria-pressed')).toBe('true');
  expect(screen.getByRole('button', { name: hd.generateCta })).toBeTruthy();
  for (const text of [hd.protected.tierNote, hd.protected.generateHint, hd.protectedUnsupported, hd.protected.launchCreate]) {
    expect(screen.queryByText(text)).toBeNull();
  }
  fireEvent.click(screen.getByRole('radio', { name: d.signingImport }));
  expect(protectedCard()).toBeNull();
  expect(screen.getByRole('textbox', { name: hd.import.label })).toBeTruthy();
  // Back on another chain, Protected is offered again.
  fireEvent.click(screen.getByRole('radio', { name: /EVM/ }));
  expect(protectedCard()).not.toBeNull();
});

it('off: a Protected tier remembered or handed off does not reach a seed wallet', async () => {
  sessionStorage.setItem('avibe.vault.addTier', 'protected');
  await open();
  expect(protectedCard()!.getAttribute('aria-pressed')).toBe('true');
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  expect(protectedCard()).toBeNull();
  expect(standardCard().getAttribute('aria-pressed')).toBe('true');
  // The memory of Protected for other keys is kept.
  expect(sessionStorage.getItem('avibe.vault.addTier')).toBe('protected');
});

it('on: the Protected seed wallet is offered', async () => {
  seedSwitch.PROTECTED_SEED_WALLET_ENABLED = true;
  await open();
  fireEvent.click(screen.getByRole('radio', { name: hd.chainMnemonic }));
  expect(protectedCard()).not.toBeNull();
  fireEvent.click(protectedCard()!);
  expect(screen.getByText(hd.protected.tierNote)).toBeTruthy();
  expect(screen.getByRole('button', { name: hd.protected.launchCreate })).toBeTruthy();
  expect(screen.queryByRole('textbox', { name: hd.import.label })).toBeNull();
});
