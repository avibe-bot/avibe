// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  createVaultAuthzWebAuthnOptions: vi.fn(),
  getVaultSandboxRootMetadata: vi.fn(),
  getVaultVmk: vi.fn(),
  completeVaultSetup: vi.fn(),
}));
const sandbox = vi.hoisted(() => ({ setup: vi.fn(), status: vi.fn() }));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('./vaultEntry', () => ({ assertVaultEntryAvailable: vi.fn(), vaultEntryIssue: () => null }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: async () => sandbox,
}));

const { useProtectedVault } = await import('./useProtectedVault');

const wrapMeta = JSON.stringify({
  v: 1,
  rp_id: 'sandbox.avibe.bot',
  copies: [{ kind: 'passkey', kdf: 'webauthn-prf-hkdf-sha256', credential_id: 'AQID' }],
});
const registration = {
  challenge_id: 'wch_1',
  credential: {
    id: 'AQID',
    rawId: 'AQID',
    type: 'public-key',
    response: { clientDataJSON: 'e30=', attestationObject: 'AA==' },
  },
};
const pendingWrapMeta = JSON.stringify({
  v: 1,
  rp_id: 'sandbox.avibe.bot',
  copies: [{ kind: 'passkey', kdf: 'webauthn-prf-hkdf-sha256', credential_id: 'BAUG' }],
});

describe('protected setup race from the create/unlock hook', () => {
  beforeEach(() => {
    api.createVaultAuthzWebAuthnOptions.mockReset().mockResolvedValue({ ok: true, requires_existing_factor: true });
    api.getVaultSandboxRootMetadata.mockReset().mockResolvedValue({ ok: true, root_metadata: {} });
    api.getVaultVmk.mockReset().mockResolvedValue({ ok: true, exists: true, wrap_meta: wrapMeta });
    api.completeVaultSetup.mockReset();
    sandbox.setup.mockReset();
    sandbox.status.mockReset().mockResolvedValue({ state: 'locked' });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('reconciles a concurrent durable root from both setup race points', async () => {
    const { result } = renderHook(() => useProtectedVault());

    // Race before the ceremony: another tab is visible in registration-options discovery.
    await act(async () => {
      await expect(result.current.setupPasskey()).rejects.toMatchObject({
        code: 'vault_already_initialized',
      });
    });
    expect(api.getVaultVmk).toHaveBeenCalledOnce();
    expect(sandbox.status).toHaveBeenCalledWith(wrapMeta);
    expect(result.current.status).toBe('locked');
    expect(sandbox.setup).not.toHaveBeenCalled();

    // Explicitly abandon that reconciled session, then model the narrower race where the other tab
    // commits after this tab received options but before its setup POST.
    api.getVaultVmk.mockResolvedValueOnce({ ok: true, exists: false, wrap_meta: null });
    await act(async () => {
      await result.current.discardAndRefresh();
    });
    api.createVaultAuthzWebAuthnOptions.mockResolvedValue({ ok: true, requires_existing_factor: false });
    sandbox.setup.mockResolvedValue({
      wrapMeta: pendingWrapMeta,
      authzRegistration: registration,
      state: 'unlocked',
    });
    api.completeVaultSetup.mockResolvedValue({ ok: false, code: 'vault_already_initialized' });

    await act(async () => {
      await expect(result.current.setupPasskey()).rejects.toMatchObject({
        code: 'vault_already_initialized',
      });
    });
    expect(sandbox.setup).toHaveBeenCalledOnce();
    expect(api.completeVaultSetup).toHaveBeenCalledOnce();
    expect(api.getVaultVmk).toHaveBeenCalledTimes(3);
    expect(sandbox.status).toHaveBeenLastCalledWith(wrapMeta);
    expect(result.current.status).toBe('locked');
  });
});
