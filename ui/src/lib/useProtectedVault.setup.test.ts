// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';

const sandbox = vi.hoisted(() => ({ setup: vi.fn() }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: async () => sandbox,
}));

const { setupVaultDurably, vaultFreshSetup } = await import('./useProtectedVault');

const wrapMeta = JSON.stringify({ v: 1, rp_id: 'sandbox.avibe.bot', copies: [{ kind: 'passkey', kdf: 'webauthn-prf-hkdf-sha256', credential_id: 'AQID' }] });
const registration = { challenge_id: 'wch_1', credential: { id: 'AQID', rawId: 'AQID', type: 'public-key', response: { clientDataJSON: 'e30=', attestationObject: 'AA==' } } };
const api = () => ({
  createVaultAuthzWebAuthnOptions: vi.fn(async () => ({ ok: true }) as never),
  getVaultSandboxRootMetadata: vi.fn(async () => ({ ok: true, root_metadata: {} }) as never),
  completeVaultSetup: vi.fn(async () => ({ ok: true, exists: true, wrap_meta: wrapMeta })),
});

describe('setupVaultDurably (contract v1.1 §G)', () => {
  beforeEach(() => {
    sandbox.setup.mockReset();
  });

  it('runs the sandbox setup, commits root and factor together, and leaves a set-up (not fresh) Vault', async () => {
    sandbox.setup.mockResolvedValue({ wrapMeta, rpId: 'sandbox.avibe.bot', credentialId: 'AQID', authzRegistration: registration, state: 'unlocked' });
    const client = api();
    await expect(setupVaultDurably(client)).resolves.toEqual({ ok: true });
    expect(client.completeVaultSetup).toHaveBeenCalledExactlyOnceWith({ wrap_meta: wrapMeta, authz_factor_registration: registration });
    // Durable now: a later first protected key must not send this registration again.
    expect(vaultFreshSetup()).toBe(false);
    // And a second call refuses rather than enrolling another credential.
    await expect(setupVaultDurably(client)).resolves.toEqual({ ok: false, code: 'vault_already_initialized' });
    expect(sandbox.setup).toHaveBeenCalledTimes(1);
  });
});
