// @vitest-environment jsdom
// AUTH-SETUP-137: real consumers, preparation and client; only the daemon/transport is inert.
import { webcrypto } from 'node:crypto';
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { VaultRequest } from '@/context/ApiContext';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { VaultApprovalCard } from '@/components/ui/vault-approval-card';
import { getVaultSandboxClient, openVaultAuthorizationWindow, resetVaultSandboxClient, VaultSandboxClient } from './vaultSandboxClient';
import { useProtectedVault, useVaultSandboxWarm } from './useProtectedVault';
import { vaultEntryIssue } from './vaultEntry';
import { stubReleaseFetch } from './__fixtures__/vaultSandboxTransport';

const api = vi.hoisted(() => ({
  getVaultSettings: vi.fn(async () => ({ ok: true, settings: {} })),
  getVaultVmk: vi.fn(async () => ({ ok: true, exists: false })),
  createVaultAgentBindingsBatch: vi.fn(async () => ({ ok: false })),
  listVaultSecrets: vi.fn(async () => ({ secrets: [] })),
  denyVaultRequest: vi.fn(async () => ({ ok: true })),
  signVaultDigest: vi.fn(async () => ({ ok: true })),
}));
const authorization = vi.hoisted(() => ({ capabilities: { can_use_vault_secrets: true, can_manage_instance: true } }));
vi.mock('@/context/ApiContext', async (original) => ({
  ...await original<typeof import('@/context/ApiContext')>(), useApi: () => api,
}));
vi.mock('@/context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => authorization }));
const request: VaultRequest = {
  id: 'public-fixture', request_type: 'access', secret_name: 'TEST_TOKEN', status: 'pending', requester: {},
  message_id: null, created_at: '', decided_at: null, expires_at: null, delivery: {},
  card: { request_type: 'access', protection: 'protected', secret_names: ['TEST_TOKEN'], protected_secret_names: ['TEST_TOKEN'],
    grant_options: [{ grant_id: 'test-grant', unlock_material: [{ name: 'TEST_TOKEN', envelope: { ciphertext: 'invalid', nonce: 'invalid', wrap_meta: '{}' } }] }] },
};
const sign: VaultRequest = { ...request, request_type: 'sign', delivery: { digest: '00'.repeat(32), scheme: 'ecdsa-secp256k1-recoverable' },
  card: { request_type: 'sign', protection: 'protected', secret_name: 'TEST_KEY', kind: 'keypair' } };
const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
function origin(value: string, secure = true) {
  vi.stubGlobal('location', new URL(value));
  vi.stubGlobal('isSecureContext', secure);
}
beforeEach(() => { stubReleaseFetch(); vi.spyOn(window, 'open').mockReturnValue(null); authorization.capabilities.can_manage_instance = true; });
afterEach(() => { cleanup(); resetVaultSandboxClient(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

// A late integrity/RPC rejection is not enough: the entry gate must prevent even inert
// popups, asset fetches, context issuance and the warm-up iframe. Existing copy tests
// never mount approval's effects or call the real client.
describe('unsupported Vault entries', () => {
  it.each([
    'http://localhost:5123', 'http://127.0.0.1:5123', 'http://127.0.0.1:15130', 'http://[::1]:5123',
    'https://localhost:5123', 'https://127.0.0.1:15130', 'https://[::1]:5123',
    'https://avibe.bot', 'https://demo.avibe.bot:443', 'https://one.two.avibe.bot',
  ])('keeps the CSP/RPC-supported %s entry available without probing parent credentials', (url) => {
    origin(url);
    vi.stubGlobal('PublicKeyCredential', undefined);
    vi.stubGlobal('navigator', undefined);
    expect(vaultEntryIssue()).toBeNull();
    expect(openVaultAuthorizationWindow()).toBeNull(); // Popup blocked by the fixture, not the entry gate.
    expect(window.open).toHaveBeenCalledOnce();
  });

  // Moved from the private capability helper to the production assessment boundary.
  // Restoring only WebCrypto clears a browser failure, but never legitimizes LAN HTTP.
  it.each([
    { url: 'http://127.0.0.1:5123', crypto: undefined, issue: 'browser' },
    { url: 'https://demo.avibe.bot', crypto: { subtle: undefined }, issue: 'browser' },
    { url: 'http://192.168.2.3:5123', crypto: { subtle: undefined }, issue: 'address' },
  ])('fails closed without parent WebCrypto at $url', async ({ url, crypto, issue }) => {
    origin(url);
    vi.stubGlobal('crypto', crypto);
    expect(vaultEntryIssue()).toBe(issue);
    await expect(getVaultSandboxClient()).rejects.toMatchObject({ code: issue === 'browser' ? 'sandbox_browser_unavailable' : 'sandbox_entry_unavailable' });
    expect(fetch).not.toHaveBeenCalled();
    vi.stubGlobal('crypto', webcrypto);
    expect(vaultEntryIssue()).toBe(issue === 'address' ? 'address' : null);
  });
  it.each([
    ['http://192.0.2.1:5123', false], ['http://192.0.2.1:5123', true],
    ['https://custom.example', true], ['https://192.0.2.1', true], ['https://demo.avibe.bot:8443', true],
    ['http://demo.avibe.bot', true], ['https://avibe.bot.attacker.example', true],
    ['http://127.0.0.1:5123', false],
  ])('rejects direct client and preopen before any side effect at %s (secure=%s)', async (url, secure) => {
    origin(url as string, secure as boolean);
    expect(() => openVaultAuthorizationWindow()).toThrow();
    const code = url === 'http://127.0.0.1:5123' ? 'sandbox_browser_unavailable' : 'sandbox_entry_unavailable';
    await expect(getVaultSandboxClient()).rejects.toMatchObject({ code });
    await expect(VaultSandboxClient.create()).rejects.toMatchObject({ code });
    expect(window.open).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    expect(document.querySelector('iframe')).toBeNull();
  });

  it('defends direct warm-up and refresh callers before discovery or transport', async () => {
    origin('http://192.0.2.1:5123', false);
    const { result } = renderHook(() => ({ warm: useVaultSandboxWarm(true), vault: useProtectedVault() }), {
      wrapper: ({ children }) => <I18nextProvider i18n={i18n}>{children}</I18nextProvider>,
    });
    await act(async () => { await result.current.vault.refresh(); });
    expect(result.current.warm).toBe(true); // No endless preparing state.
    expect(result.current.vault.status).toBe('error');
    expect(api.getVaultVmk).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    expect(window.open).not.toHaveBeenCalled();
    expect(document.querySelector('iframe')).toBeNull();
  });

  it.each(['en', 'zh'] as const)('AUTH-SETUP-137 shows early %s access/sign guidance, disables only protected approval, and preserves deny', async (language) => {
    origin('http://192.0.2.1:5123', false);
    await i18n.changeLanguage(language);
    const copy = language === 'zh' ? zh : en;
    for (const current of [request, sign, { ...request, card: { request_type: 'access', protection: 'protected' } }]) {
      const resolved = vi.fn();
      await act(async () => { render(<MemoryRouter><I18nextProvider i18n={i18n}><VaultApprovalCard request={current} onResolved={resolved} onCancel={vi.fn()} /></I18nextProvider></MemoryRouter>); });
      expect(screen.getByText(copy.vaults.entry.ownerHelp).closest('[role="alert"]')).toBeTruthy();
      const approve = screen.getByRole('button', { name: current.request_type === 'sign' ? copy.vaults.approval.sign : copy.vaults.approval.approve });
      expect((approve as HTMLButtonElement).disabled).toBe(true);
      expect(approve.querySelector('.animate-spin')).toBeNull();
      expect(screen.getByRole('link').getAttribute('href')).toBe('/settings/remote-access');
      fireEvent.click(approve);
      expect(api.createVaultAgentBindingsBatch).not.toHaveBeenCalled();
      expect(api.getVaultVmk).not.toHaveBeenCalled();
      expect(window.open).not.toHaveBeenCalled();
      expect(fetch).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole('button', { name: copy.vaults.approval.deny }));
      await vi.waitFor(() => expect(resolved).toHaveBeenCalledWith({ kind: 'denied', requestType: current.request_type }));
      cleanup();
    }
  });

  it('AUTH-SETUP-137 gives non-owners owner guidance without a forbidden settings link; standard signing still works', async () => {
    origin('https://custom.example');
    await i18n.changeLanguage('en');
    authorization.capabilities.can_manage_instance = false;
    const resolved = vi.fn();
    const view = render(<MemoryRouter><I18nextProvider i18n={i18n}><VaultApprovalCard request={request} onResolved={resolved} onCancel={vi.fn()} /></I18nextProvider></MemoryRouter>);
    expect(screen.getByRole('alert').textContent).toMatch(/owner/i);
    expect(screen.queryByRole('link')).toBeNull();
    view.rerender(<MemoryRouter><I18nextProvider i18n={i18n}><VaultApprovalCard request={{ ...sign, card: { ...sign.card, protection: 'standard' } }} onResolved={resolved} onCancel={vi.fn()} /></I18nextProvider></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: en.vaults.approval.sign }));
    await vi.waitFor(() => expect(api.signVaultDigest).toHaveBeenCalledOnce());
    expect(resolved).toHaveBeenCalledWith({ kind: 'approved', requestType: 'sign' });
    expect(fetch).not.toHaveBeenCalled();
  });
});
