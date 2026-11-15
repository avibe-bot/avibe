// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  createVaultAuthzWebAuthnOptions: vi.fn(),
  getVaultSandboxRootMetadata: vi.fn(),
  completeVaultSetup: vi.fn(),
}));
const sandbox = vi.hoisted(() => ({ setup: vi.fn() }));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
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

describe('protected setup from the create/unlock hook', () => {
  beforeEach(() => {
    api.createVaultAuthzWebAuthnOptions.mockReset().mockResolvedValue({ ok: true });
    api.getVaultSandboxRootMetadata.mockReset().mockResolvedValue({ ok: true, root_metadata: {} });
    api.completeVaultSetup.mockReset().mockResolvedValue({ ok: true, exists: true, wrap_meta: wrapMeta });
    sandbox.setup.mockReset().mockResolvedValue({
      wrapMeta,
      rpId: 'sandbox.avibe.bot',
      credentialId: 'AQID',
      authzRegistration: registration,
      state: 'unlocked',
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('does not report durable readiness until the root and factor POST resolves', async () => {
    type SetupResult = { ok: boolean; exists?: boolean; wrap_meta?: string };
    let resolveCommit!: (value: SetupResult) => void;
    api.completeVaultSetup.mockImplementation(() => new Promise<SetupResult>((resolve) => {
      resolveCommit = resolve;
    }));
    const { result } = renderHook(() => useProtectedVault());
    let setupPromise!: Promise<void>;

    await act(async () => {
      setupPromise = result.current.setupPasskey();
      await vi.waitFor(() => expect(api.completeVaultSetup).toHaveBeenCalledOnce());
    });

    expect(result.current.status).not.toBe('unlocked');
    expect(vaultUnlocked()).toBe(false);
    expect(vaultFreshSetup()).toBe(true);

    await act(async () => {
      resolveCommit({ ok: true, exists: true, wrap_meta: wrapMeta });
      await setupPromise;
    });
    expect(result.current.status).toBe('unlocked');
    expect(vaultUnlocked()).toBe(true);
    expect(vaultFreshSetup()).toBe(false);
    expect(sandbox.setup).toHaveBeenCalledOnce();
    expect(api.completeVaultSetup).toHaveBeenCalledExactlyOnceWith({
      wrap_meta: wrapMeta,
      authz_factor_registration: registration,
    });
  });
});
