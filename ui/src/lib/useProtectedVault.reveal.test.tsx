// @vitest-environment jsdom

// `revealProtectedSecret`: one field keeps the original single-record `reveal` request, which every
// pinned sandbox accepts; several go as `items` with the default field's label (a build that lists them).
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { useProtectedVault } from './useProtectedVault';

const WRAP = JSON.stringify({ v: 1, copies: [{ kind: 'passkey' }] });
const api = vi.hoisted(() => ({ getVaultVmk: vi.fn() }));
const client = vi.hoisted(() => ({ status: vi.fn(), reveal: vi.fn() }));
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
  client.reveal.mockResolvedValue({ completed: true });
  const { result } = renderHook(() => useProtectedVault());
  await act(async () => { await result.current.refresh(); });
  return result;
};
const envelope = { ciphertext: 'c', nonce: 'n', wrap_meta: WRAP };
const item = (name: string) => ({ material: { name, kind: 'static' as const, envelope }, context: { name } as never });

it('one field: the original single-record request', async () => {
  const result = await lockedVault();
  await act(async () => { await result.current.revealProtectedSecret([item('LOGIN')], 'password', 'win1'); });
  expect(client.reveal).toHaveBeenCalledExactlyOnceWith(item('LOGIN'), 'win1');
});

it('several fields: every record as items, in order, with the default field label', async () => {
  const result = await lockedVault();
  const items = [item('LOGIN'), item('LOGIN.username'), item('LOGIN.totp')];
  await act(async () => { await result.current.revealProtectedSecret(items, 'password', 'win1'); });
  expect(client.reveal).toHaveBeenCalledExactlyOnceWith({ items, defaultFieldLabel: 'password' }, 'win1');
});

it('no field: refuses before any sandbox work', async () => {
  const result = await lockedVault();
  await expect(result.current.revealProtectedSecret([], 'password', 'win1')).rejects.toThrow('reveal-needs-a-field');
  expect(client.reveal).not.toHaveBeenCalled();
});
