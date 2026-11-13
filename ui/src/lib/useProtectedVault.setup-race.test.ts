// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

const wrapMeta = JSON.stringify({ v: 1, rp_id: 'sandbox.avibe.bot', copies: [{ kind: 'passkey', kdf: 'webauthn-prf-hkdf-sha256', credential_id: 'AQID' }] });
const registration = { challenge_id: 'wch_1', credential: { id: 'AQID', rawId: 'AQID', type: 'public-key', response: { clientDataJSON: 'e30=', attestationObject: 'AA==' } } };

// A stand-in for the sandbox client singleton: each handshake makes a new client, and a reset
// destroys the live one (as resetVaultSandboxClient does with the real iframe).
const sandbox = vi.hoisted(() => {
  type Client = { id: number; destroyed: boolean; setup: ReturnType<typeof vi.fn> };
  const state = { handshakes: 0, current: null as Client | null, all: [] as Client[] };
  return {
    state,
    get: async () => {
      if (!state.current) {
        state.handshakes += 1;
        state.current = { id: state.handshakes, destroyed: false, setup: vi.fn() };
        state.all.push(state.current);
      }
      return state.current;
    },
    reset: vi.fn(() => {
      if (state.current) state.current.destroyed = true;
      state.current = null;
    }),
  };
});
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: sandbox.get,
  resetVaultSandboxClient: sandbox.reset,
}));

const { setupVaultDurably, vaultFreshSetup, vaultUnlocked } = await import('./useProtectedVault');

describe('setupVaultDurably when another tab won the setup race', () => {
  it('destroys the sandbox client holding the uncommitted root, so the next ceremony handshakes fresh', async () => {
    const first = await sandbox.get();
    first.setup.mockResolvedValue({ wrapMeta, rpId: 'sandbox.avibe.bot', credentialId: 'AQID', authzRegistration: registration, state: 'unlocked' });
    const api = {
      createVaultAuthzWebAuthnOptions: vi.fn(async () => ({ ok: true }) as never),
      getVaultSandboxRootMetadata: vi.fn(async () => ({ ok: true, root_metadata: {} }) as never),
      completeVaultSetup: vi.fn(async () => ({ ok: false, code: 'vault_already_initialized' })),
    };
    await expect(setupVaultDurably(api)).resolves.toEqual({ ok: false, code: 'vault_already_initialized' });

    // Parent and sandbox both forget the losing root.
    expect(sandbox.reset).toHaveBeenCalledOnce();
    expect(first.destroyed).toBe(true);
    expect(vaultFreshSetup()).toBe(false);
    expect(vaultUnlocked()).toBe(false);

    // A later ceremony goes to a new client (fresh handshake), never the stale unlocked one.
    const next = await sandbox.get();
    expect(next).not.toBe(first);
    expect(sandbox.state.handshakes).toBe(2);
    next.setup.mockRejectedValue(Object.assign(new Error('x'), { code: 'passkey_cancelled' }));
    await expect(setupVaultDurably(api)).rejects.toMatchObject({ code: 'passkey_cancelled' });
    expect(next.setup).toHaveBeenCalledOnce();
    expect(first.setup).toHaveBeenCalledOnce();
  });
});
