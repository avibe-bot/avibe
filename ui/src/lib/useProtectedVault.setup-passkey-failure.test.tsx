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
const sandboxEvents = vi.hoisted(() => ({
  listener: null as ((event: { state: 'locked'; reason: 'auto-lock' }) => void) | null,
}));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('./vaultEntry', () => ({ assertVaultEntryAvailable: vi.fn(), vaultEntryIssue: () => null }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  subscribeVaultStateEvents: (listener: (event: { state: 'locked'; reason: 'auto-lock' }) => void) => {
    sandboxEvents.listener = listener;
    return () => {};
  },
  getVaultSandboxClient: async () => sandbox,
}));

const { useProtectedVault, vaultFreshSetup, vaultUnlocked } = await import('./useProtectedVault');

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

describe('protected setup failure from the create/unlock hook', () => {
  beforeEach(() => {
    api.createVaultAuthzWebAuthnOptions.mockReset().mockResolvedValue({ ok: true });
    api.getVaultSandboxRootMetadata.mockReset().mockResolvedValue({ ok: true, root_metadata: {} });
    api.getVaultVmk.mockReset().mockResolvedValue({ ok: true, exists: false });
    api.completeVaultSetup.mockReset().mockResolvedValue({ ok: false, code: 'invalid_protected_authz' });
    sandbox.setup.mockReset().mockResolvedValue({
      wrapMeta,
      rpId: 'sandbox.avibe.bot',
      credentialId: 'AQID',
      authzRegistration: registration,
      state: 'unlocked',
    });
    sandbox.status.mockReset().mockResolvedValue({ state: 'unlocked', expiresAt: 123456 });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('does not report the Vault unlocked and preserves the same-proof retry across discovery refresh', async () => {
    api.completeVaultSetup.mockResolvedValue({ ok: false });
    const { result } = renderHook(() => useProtectedVault());

    await expect(act(async () => result.current.setupPasskey())).rejects.toMatchObject({
      code: 'setup_failed',
    });
    expect(result.current.status).not.toBe('unlocked');
    expect(vaultUnlocked()).toBe(false);
    expect(vaultFreshSetup()).toBe(true);

    await act(async () => {
      sandboxEvents.listener?.({ state: 'locked', reason: 'auto-lock' });
    });
    expect(vaultUnlocked()).toBe(false);
    expect(vaultFreshSetup()).toBe(true);

    await act(async () => result.current.refresh());
    expect(result.current.status).toBe('needs-setup');
    expect(vaultUnlocked()).toBe(false);
    expect(vaultFreshSetup()).toBe(true);

    api.completeVaultSetup.mockResolvedValue({ ok: true, exists: true, wrap_meta: wrapMeta });
    await act(async () => result.current.setupPasskey());
    expect(sandbox.setup).toHaveBeenCalledOnce();
    expect(api.completeVaultSetup).toHaveBeenCalledTimes(2);
    expect(vaultFreshSetup()).toBe(false);
    expect(result.current.status).toBe('unlocked');
  });
});
