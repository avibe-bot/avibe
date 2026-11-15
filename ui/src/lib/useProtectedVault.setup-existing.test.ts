// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

const sandbox = vi.hoisted(() => ({ setup: vi.fn() }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: async () => sandbox,
}));

const { setupVaultDurably, vaultFreshSetup } = await import('./useProtectedVault');

describe('setupVaultDurably when the server already has a Passkey', () => {
  it('starts no ceremony (no second credential) and reports it as set up elsewhere', async () => {
    const completeVaultSetup = vi.fn();
    const result = await setupVaultDurably({
      // Another tab committed meanwhile: fresh registration options say a factor already exists.
      createVaultAuthzWebAuthnOptions: vi.fn(async () => ({ ok: true, requires_existing_factor: true }) as never),
      getVaultSandboxRootMetadata: vi.fn(async () => ({ ok: true, root_metadata: {} }) as never),
      completeVaultSetup,
    });
    expect(result).toEqual({ ok: false, code: 'vault_already_initialized' });
    expect(sandbox.setup).not.toHaveBeenCalled();
    expect(completeVaultSetup).not.toHaveBeenCalled();
    expect(vaultFreshSetup()).toBe(false);
  });
});
