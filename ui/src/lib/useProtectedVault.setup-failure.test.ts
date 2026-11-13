// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

const sandbox = vi.hoisted(() => ({ setup: vi.fn() }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: async () => sandbox,
}));

const { setupVaultDurably, vaultFreshSetup } = await import('./useProtectedVault');

const wrapMeta = JSON.stringify({ v: 1, rp_id: 'sandbox.avibe.bot', copies: [{ kind: 'passkey', kdf: 'webauthn-prf-hkdf-sha256', credential_id: 'AQID' }] });
const registration = { challenge_id: 'wch_1', credential: { id: 'AQID', rawId: 'AQID', type: 'public-key', response: { clientDataJSON: 'e30=', attestationObject: 'AA==' } } };

describe('setupVaultDurably when the server refuses', () => {
  it('reports the code and keeps nothing durable (the tab stays a fresh, unsaved setup)', async () => {
    sandbox.setup.mockResolvedValue({ wrapMeta, rpId: 'sandbox.avibe.bot', credentialId: 'AQID', authzRegistration: registration, state: 'unlocked' });
    const result = await setupVaultDurably({
      createVaultAuthzWebAuthnOptions: vi.fn(async () => ({ ok: true }) as never),
      getVaultSandboxRootMetadata: vi.fn(async () => ({ ok: true, root_metadata: {} }) as never),
      completeVaultSetup: vi.fn(async () => ({ ok: false, code: 'vault_policy_factor_unavailable' })),
    });
    expect(result).toEqual({ ok: false, code: 'vault_policy_factor_unavailable' });
    expect(vaultFreshSetup()).toBe(true);
  });

  it('commits the same uncommitted setup again on retry, without a second Passkey ceremony', async () => {
    const completeVaultSetup = vi.fn(async () => ({ ok: true, exists: true, wrap_meta: wrapMeta }));
    await expect(setupVaultDurably({
      createVaultAuthzWebAuthnOptions: vi.fn(async () => ({ ok: true }) as never),
      getVaultSandboxRootMetadata: vi.fn(async () => ({ ok: true, root_metadata: {} }) as never),
      completeVaultSetup,
    })).resolves.toEqual({ ok: true });
    expect(sandbox.setup).toHaveBeenCalledTimes(1);
    expect(completeVaultSetup).toHaveBeenCalledExactlyOnceWith({ wrap_meta: wrapMeta, authz_factor_registration: registration });
    expect(vaultFreshSetup()).toBe(false);
  });
});

describe('setupVaultDurably cancelled', () => {
  it('passes a cancelled Passkey prompt through as an error, before anything is posted', async () => {
    sandbox.setup.mockRejectedValue(Object.assign(new Error('x'), { code: 'passkey_cancelled' }));
    vi.resetModules();
    const fresh = await import('./useProtectedVault');
    const completeVaultSetup = vi.fn();
    await expect(fresh.setupVaultDurably({
      createVaultAuthzWebAuthnOptions: vi.fn(async () => ({ ok: true }) as never),
      getVaultSandboxRootMetadata: vi.fn(async () => ({ ok: true, root_metadata: {} }) as never),
      completeVaultSetup,
    })).rejects.toMatchObject({ code: 'passkey_cancelled' });
    expect(completeVaultSetup).not.toHaveBeenCalled();
  });
});

