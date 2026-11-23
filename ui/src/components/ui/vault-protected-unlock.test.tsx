// @vitest-environment jsdom

import { webcrypto } from 'node:crypto';
import type { ComponentProps } from 'react';
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { VaultProtectedUnlock } from './vault-protected-unlock';

type Vault = ComponentProps<typeof VaultProtectedUnlock>['vault'];

function lockedVault(status: 'needs-setup' | 'locked'): Vault {
  const forbidden = vi.fn(async (): Promise<never> => { throw new Error('No credential or key operations in this test'); });
  return {
    status,
    error: null,
    setError: vi.fn(),
    refresh: forbidden,
    setupPasskey: forbidden,
    unlockPasskey: forbidden,
    sealValue: forbidden,
    signProtectedRequest: forbidden,
    approveProtectedRelease: forbidden,
    revealProtectedSecret: forbidden,
    afterCreated: forbidden,
    lock: forbidden,
    lockAndResetForPolicyChange: forbidden,
    discardAndRefresh: forbidden,
    hasPasskey: () => status === 'locked',
    passkeyUsableHere: () => status === 'locked',
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Both statuses use the shared entry notice. Independent EN/ZH expectations protect
// the loopback/remote distinction; key parity cannot detect misleading advice.
// Make all other prerequisites valid, then restore only WebCrypto to prove it —
// not an earlier address/secure-context rejection — selected this notice.
const locales = [
  {
    lng: 'en',
    title: 'Protected Vault is unavailable in this browser context',
    setup: 'Add a passkey',
    unlock: 'Unlock with passkey',
    guidance: 'Use a compatible browser. On the computer running Avibe, open Vaults from localhost or 127.0.0.1; on other devices, use your HTTPS avibe.bot address. LAN HTTP addresses (such as 192.168.x.x) are not supported.',
  },
  {
    lng: 'zh',
    title: '当前浏览器环境无法使用受保护的 Vault',
    setup: '添加 passkey',
    unlock: '用 passkey 解锁',
    guidance: '请使用兼容的浏览器。在运行 Avibe 的电脑上，通过 localhost 或 127.0.0.1 打开 Vaults；在其他设备上，使用你的 HTTPS avibe.bot 地址。不支持局域网 HTTP 地址（如 192.168.x.x）。',
  },
] as const;

describe('entry notice takes precedence over stale vault status', () => {
  it.each(['checking', 'error', 'unlocked'] as const)('does not spin, retry or claim availability for %s at LAN HTTP', async (status) => {
    vi.stubGlobal('location', new URL('http://192.0.2.1:5123'));
    const i18n = createInstance();
    await i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } } });
    const vault = { ...lockedVault('locked'), status };
    const dismiss = vi.fn();
    render(<I18nextProvider i18n={i18n}><VaultProtectedUnlock vault={vault} onDismiss={dismiss} /></I18nextProvider>);
    expect(screen.getByText(en.vaults.entry.addressTitle)).toBeTruthy();
    expect(screen.queryByText(en.vaults.protectedUnlock.checking)).toBeNull();
    expect(screen.queryByText(en.vaults.protectedUnlock.unlocked)).toBeNull();
    expect(screen.queryByText(en.vaults.protectedUnlock.retry)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: en.vaults.protectedUnlock.cancel }));
    expect(dismiss).toHaveBeenCalledOnce();
    expect(vault.setupPasskey).not.toHaveBeenCalled();
  });
});

// A desktop app whose in-app passkey step failed may have left this page checking, unlocked or in
// error. Nothing runs here any more, so the panel offers the browser, where the step starts over.
describe('browser hand-off takes precedence over stale vault status', () => {
  afterEach(() => {
    Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
  });

  it.each(['checking', 'error', 'unlocked'] as const)('offers the browser instead of %s in the desktop app', async (status) => {
    vi.stubGlobal('location', new URL('http://127.0.0.1:5123'));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    const i18n = createInstance();
    await i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } } });
    const vault = { ...lockedVault('locked'), status };
    const continueInBrowser = vi.fn();
    render(<I18nextProvider i18n={i18n}><VaultProtectedUnlock vault={vault} onContinueInBrowser={continueInBrowser} /></I18nextProvider>);
    expect(screen.getByText(en.vaults.protectedUnlock.browserNote)).toBeTruthy();
    expect(screen.queryByText(en.vaults.protectedUnlock.checking)).toBeNull();
    expect(screen.queryByText(en.vaults.protectedUnlock.unlocked)).toBeNull();
    expect(screen.queryByText(en.vaults.protectedUnlock.retry)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: en.vaults.protectedUnlock.openInBrowser }));
    expect(continueInBrowser).toHaveBeenCalledOnce();
  });
});

describe.each(['needs-setup', 'locked'] as const)('protected vault %s entry guidance', (status) => {
  it.each(locales)('renders actionable, loopback-aware $lng guidance only while WebCrypto is missing, without starting a ceremony', async ({ lng, guidance, title, setup, unlock }) => {
    vi.stubGlobal('location', new URL('http://127.0.0.1:5123'));
    vi.stubGlobal('isSecureContext', true);
    vi.stubGlobal('crypto', { subtle: undefined });
    const i18n = createInstance();
    await i18n.use(initReactI18next).init({
      lng,
      fallbackLng: false,
      resources: { en: { translation: en }, zh: { translation: zh } },
      interpolation: { escapeValue: false },
    });
    const vault = lockedVault(status);
    const panel = <I18nextProvider i18n={i18n}><VaultProtectedUnlock vault={vault} /></I18nextProvider>;
    const view = render(panel);

    expect(screen.getByText(title)).toBeTruthy();
    expect(screen.getByText(guidance)).toBeTruthy();
    expect(screen.queryAllByRole('button')).toEqual([]);
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(vault.setupPasskey).not.toHaveBeenCalled();

    vi.stubGlobal('crypto', webcrypto);
    // A new element forces the real component to reassess; no helper is mocked.
    view.rerender(<I18nextProvider i18n={i18n}><VaultProtectedUnlock vault={vault} /></I18nextProvider>);
    expect(screen.queryByText(guidance)).toBeNull();
    expect(screen.queryByText(title)).toBeNull();
    const action = screen.getByRole('button', { name: status === 'needs-setup' ? setup : unlock }) as HTMLButtonElement;
    if (status === 'needs-setup') {
      expect(action.disabled).toBe(true);
      fireEvent.click(screen.getByRole('checkbox'));
    }
    expect(action.disabled).toBe(false);
    // lockedVault shares one forbidden spy across discovery, setup, unlock and
    // every key/value operation, so this asserts all stay untouched after recovery.
    expect(vault.setupPasskey).not.toHaveBeenCalled();
  });
});

describe('a passkey without the encryption Protected needs', () => {
  it.each([
    ['en', en, 'needs-setup', en.vaults.protectedUnlock.errors.prfUnavailable],
    ['en', en, 'locked', en.vaults.protectedUnlock.errors.prfUnavailableUnlock],
    ['zh', zh, 'needs-setup', zh.vaults.protectedUnlock.errors.prfUnavailable],
    ['zh', zh, 'locked', zh.vaults.protectedUnlock.errors.prfUnavailableUnlock],
  ] as const)('(%s, %s): setup says where to save a new passkey; unlock says the vault is still locked', async (lng, translation, status, expected) => {
    vi.stubGlobal('location', new URL('http://127.0.0.1:5123'));
    vi.stubGlobal('isSecureContext', true);
    vi.stubGlobal('crypto', webcrypto);
    const i18n = createInstance();
    await i18n.use(initReactI18next).init({ lng, resources: { [lng]: { translation } } });
    render(<I18nextProvider i18n={i18n}><VaultProtectedUnlock vault={{ ...lockedVault(status), error: 'passkey-prf-unavailable' }} /></I18nextProvider>);
    expect(screen.getByText(expected)).toBeTruthy();
  });
});

// When a failed in-app step moves the panel to the browser hand-off, the control the user pressed
// is gone (a seed-phrase wallet's launch button, Add a passkey) and focus falls to the page. It
// lands on the hand-off's action, described by its sentence; focus that is elsewhere stays there.
describe('browser hand-off after an in-app failure', () => {
  async function fallbackPanel() {
    vi.resetModules();
    vi.stubGlobal('location', new URL('http://127.0.0.1:5123'));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { configurable: true, value: true });
    const { VaultProtectedUnlock: Panel } = await import('./vault-protected-unlock');
    const { recordVaultPasskeyOutcome } = await import('@/lib/vaultBrowserHandoff');
    recordVaultPasskeyOutcome({ code: 'seal_hd_failed' });
    const i18n = createInstance();
    await i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } } });
    return (vault: Vault) => render(<I18nextProvider i18n={i18n}><Panel vault={vault} onContinueInBrowser={vi.fn()} /></I18nextProvider>);
  }

  afterEach(() => {
    Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
    Reflect.deleteProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__');
  });

  it('lands focus that fell to the page on the hand-off action', async () => {
    const renderPanel = await fallbackPanel();
    (document.activeElement as HTMLElement | null)?.blur();
    renderPanel({ ...lockedVault('locked'), status: 'unlocked' });
    const action = screen.getByRole('button', { name: en.vaults.protectedUnlock.openInBrowser });
    expect(document.activeElement).toBe(action);
    expect(action.getAttribute('aria-describedby')).toBe(screen.getByText(en.vaults.protectedUnlock.browserFallbackNote).id);
  });

  it('leaves focus that is elsewhere where it is', async () => {
    const renderPanel = await fallbackPanel();
    const field = document.createElement('input');
    document.body.appendChild(field);
    field.focus();
    renderPanel(lockedVault('needs-setup'));
    expect(document.activeElement).toBe(field);
    field.remove();
  });
});
