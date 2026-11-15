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

describe('protected setup failure from the create/unlock hook', () => {
  beforeEach(() => {
    api.createVaultAuthzWebAuthnOptions.mockReset().mockResolvedValue({ ok: true });
    api.getVaultSandboxRootMetadata.mockReset().mockResolvedValue({ ok: true, root_metadata: {} });
    api.completeVaultSetup.mockReset().mockResolvedValue({ ok: false, code: 'invalid_protected_authz' });
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

  it('does not report the Vault unlocked when durable setup returns an unknown refusal', async () => {
    api.completeVaultSetup.mockResolvedValue({ ok: false });
    const { result } = renderHook(() => useProtectedVault());

    await expect(act(async () => result.current.setupPasskey())).rejects.toMatchObject({
      code: 'setup_failed',
    });
    expect(result.current.status).not.toBe('unlocked');
    expect(vaultUnlocked()).toBe(false);
    expect(vaultFreshSetup()).toBe(true);
  });
});
