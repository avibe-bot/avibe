// @vitest-environment jsdom
// "Lock now" must reach the sandbox that holds the key. A desktop app whose in-app passkey step
// failed hands later steps to the browser and refuses new sandbox work on this page, but an unlock
// that succeeded here before that still lives in this page's sandbox frame until it is locked.
import { act, renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

const held = vi.hoisted(() => ({ lock: vi.fn(async () => ({ ok: true, state: 'locked' })) }));
const getClient = vi.hoisted(() => vi.fn(async () => {
  throw Object.assign(new Error('Protected Vault is unavailable in this entry context'), { code: 'sandbox_browser_required' });
}));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => ({}) }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getActiveVaultSandboxClient: () => held,
  getVaultSandboxClient: getClient,
}));

import { useVaultLock } from './useProtectedVault';

it('locks the sandbox frame that holds the key even after this page moved its steps to the browser', async () => {
  const { result } = renderHook(() => useVaultLock());
  await act(async () => {
    result.current.lockNow();
  });
  await vi.waitFor(() => expect(held.lock).toHaveBeenCalledTimes(1));
  expect(getClient).not.toHaveBeenCalled();
});
