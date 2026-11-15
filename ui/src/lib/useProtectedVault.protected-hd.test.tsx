// @vitest-environment jsdom

// `sealHdWallet` through the hook: the window gets this vault's base root, the page's own setup/root
// snapshot is rechecked before anything is sent, and the window's establishment flag passes through
// as reported (the shared setup lifecycle may keep it true after the root is committed).
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { useProtectedVault } from './useProtectedVault';

const WRAP = JSON.stringify({ v: 1, copies: [{ kind: 'passkey' }] });
const api = vi.hoisted(() => ({ getVaultVmk: vi.fn() }));
const client = vi.hoisted(() => ({ status: vi.fn(), sealHdWallet: vi.fn() }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: async () => client,
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

const lockedVault = async () => {
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('BroadcastChannel', undefined);
  api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: WRAP });
  client.status.mockResolvedValue({ state: 'locked' });
  const { result } = renderHook(() => useProtectedVault());
  await act(async () => { await result.current.refresh(); });
  return result;
};

it('sends name, mode and the base root, and passes a later true establishment flag through without discarding the result', async () => {
  const result = await lockedVault();
  const sealed = { envelope: { ciphertext: 'c', nonce: 'n', wrap_meta: 'w' }, accounts: [], expectedAccount0: {}, establishingVmk: true };
  client.sealHdWallet.mockResolvedValue(sealed);
  let out: unknown;
  await act(async () => { out = await result.current.sealHdWallet('COLD', 'create', 'win1'); });
  expect(client.sealHdWallet).toHaveBeenCalledExactlyOnceWith({ name: 'COLD', mode: 'create', wrapMeta: WRAP }, 'win1');
  // Not a fresh setup in this page, so no registration rides along; the flag is the window's.
  expect(out).toEqual({ ...sealed, authzFactorRegistration: undefined });
});

it('refuses without a protected vault, before any window work', async () => {
  vi.stubGlobal('isSecureContext', true);
  api.getVaultVmk.mockResolvedValue({ ok: true, exists: false });
  const { result } = renderHook(() => useProtectedVault());
  await act(async () => { await result.current.refresh(); });
  await expect(result.current.sealHdWallet('COLD', 'create', 'win1')).rejects.toThrow('vault-not-setup');
  expect(client.sealHdWallet).not.toHaveBeenCalled();
});
