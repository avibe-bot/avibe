/* @vitest-environment jsdom */

// The form obeys the switch as shipped, with no mock of it here: a seed-phrase wallet offers the
// Protected tier exactly when PROTECTED_SEED_WALLET_ENABLED is on. The value is not pinned, so turning
// it on stays one line; both states' behaviour is in vault-secret-form.protected-seed-switch.test.tsx.
import type { ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
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
  connectWorkbenchEvents: vi.fn(() => vi.fn()),
}));
const vault = vi.hoisted(() => ({ status: 'locked', refresh: vi.fn(), afterCreated: vi.fn(), sealValue: vi.fn(), sealHdWallet: vi.fn() }));
vi.mock('@/context/ApiContext', async (original) => ({ ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api }));
vi.mock('@/lib/useProtectedVault', () => ({ useProtectedVault: () => vault, useProtectedHdSupported: () => true, useVaultSandboxWarm: () => true }));
vi.mock('./vault-protected-unlock', () => ({ VaultProtectedUnlock: () => null }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const AsOwner = ({ children }: { children: ReactNode }) => (
  <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: null, capabilities: OWNER_INSTANCE_CAPABILITIES }}>{children}</InstanceAuthorizationContext.Provider>
);
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('a seed wallet offers Protected exactly when the shipped switch is on', async () => {
  vi.stubGlobal('isSecureContext', true);
  render(<I18nextProvider i18n={i18n}><AsOwner><VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} /></AsOwner></I18nextProvider>);
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.dialog.kindKeypair }));
  fireEvent.click(screen.getByRole('radio', { name: en.vaults.hd.chainMnemonic }));
  const offered = screen.queryByRole('button', { name: new RegExp(`^${en.vaults.dialog.protectedProtection}`) }) !== null;
  expect(offered).toBe(PROTECTED_SEED_WALLET_ENABLED);
  vi.unstubAllGlobals();
});
