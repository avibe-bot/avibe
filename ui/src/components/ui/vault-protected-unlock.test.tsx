// @vitest-environment jsdom

import type { ComponentProps } from 'react';
import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
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
    revealProtectedValue: forbidden,
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

// The real component consumes both locales in two different fallback branches.
// Parent-page tests mock this panel or its capability helper; key-parity tests
// cannot catch copy that incorrectly rejects 127.0.0.1 or recommends LAN HTTP.
// Withhold only the browser capability, not the helper or the rendered message.
const locales = [
  {
    lng: 'en',
    guidance: 'Use a compatible browser. On the computer running Avibe, open Vaults from localhost or 127.0.0.1; on other devices, use your HTTPS avibe.bot address. LAN HTTP addresses (such as 192.168.x.x) are not supported.',
  },
  {
    lng: 'zh',
    guidance: '请使用兼容的浏览器。在运行 Avibe 的电脑上，通过 localhost 或 127.0.0.1 打开 Vaults；在其他设备上，使用你的 HTTPS avibe.bot 地址。不支持局域网 HTTP 地址（如 192.168.x.x）。',
  },
] as const;

describe.each(['needs-setup', 'locked'] as const)('protected vault %s entry guidance', (status) => {
  it.each(locales)('renders actionable, loopback-aware $lng guidance without starting a ceremony', async ({ lng, guidance }) => {
    vi.stubGlobal('crypto', { subtle: undefined });
    const i18n = createInstance();
    await i18n.use(initReactI18next).init({
      lng,
      fallbackLng: false,
      resources: { en: { translation: en }, zh: { translation: zh } },
      interpolation: { escapeValue: false },
    });
    const vault = lockedVault(status);
    render(<I18nextProvider i18n={i18n}><VaultProtectedUnlock vault={vault} /></I18nextProvider>);

    expect(screen.getByText(guidance)).toBeTruthy();
    expect(screen.queryAllByRole('button')).toEqual([]);
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(vault.setupPasskey).not.toHaveBeenCalled();
  });
});
