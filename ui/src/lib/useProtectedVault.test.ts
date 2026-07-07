import { describe, expect, it, vi } from 'vitest';

import { authorizeProtectedDeleteWithSandbox, webauthnAvailable } from './useProtectedVault';
import {
  VAULT_SANDBOX_EXPECTED_BUILD_HASH,
  VAULT_SANDBOX_IFRAME_URL,
  VAULT_SANDBOX_IFRAME_RESOURCE_PATH,
  VAULT_SANDBOX_ORIGIN,
  VAULT_SANDBOX_PINNED_MANIFEST,
  VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS,
} from './vaultSandboxManifest';

describe('protected vault sandbox cutover', () => {
  it('pins the deployed sandbox build and only verifies runtime resources', () => {
    expect(VAULT_SANDBOX_ORIGIN).toBe('https://sandbox.avibe.bot');
    expect(VAULT_SANDBOX_EXPECTED_BUILD_HASH).toBe('dev');
    expect(VAULT_SANDBOX_IFRAME_RESOURCE_PATH).toBe('/index.html');
    expect(VAULT_SANDBOX_IFRAME_URL).toBe(`${VAULT_SANDBOX_ORIGIN}${VAULT_SANDBOX_IFRAME_RESOURCE_PATH}`);
    expect(VAULT_SANDBOX_PINNED_MANIFEST.resources['/index.html']).toMatch(/^sha256-/);
    expect(VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS).toContain(VAULT_SANDBOX_IFRAME_RESOURCE_PATH);
    expect(VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS.every((path) => !path.endsWith('.map'))).toBe(true);
  });

  it('fails closed outside a browser context', () => {
    expect(webauthnAvailable()).toBe(false);
  });

  it('authorizes protected delete without requiring unlocked VMK state', async () => {
    const credential = {
      id: 'cred-1',
      rawId: 'cred-1',
      type: 'public-key',
      response: { clientDataJSON: 'client', authenticatorData: 'auth', signature: 'sig' },
    };
    const api = {
      createVaultDeleteChallenge: vi.fn(async () => ({
        ok: true,
        challenge_id: 'vop_delete',
        webauthn: {
          challenge: 'challenge',
          rpId: 'sandbox.avibe.bot',
          userVerification: 'required',
          allowCredentials: [{ type: 'public-key', id: 'cred-1', factor_id: 'vaf_delete' }],
        },
      })),
    };
    const client = {
      deleteAuthzAssertion: vi.fn(async () => ({ challengeId: 'vop_delete', assertion: credential })),
    };

    await expect(
      authorizeProtectedDeleteWithSandbox('PROTECTED_KEY', api, async () => client as never),
    ).resolves.toEqual({
      kind: 'webauthn',
      challenge_id: 'vop_delete',
      factor_id: 'vaf_delete',
      assertion: credential,
    });
    expect(api.createVaultDeleteChallenge).toHaveBeenCalledWith('PROTECTED_KEY', { handleError: false });
    expect(client.deleteAuthzAssertion).toHaveBeenCalledWith({
      challengeId: 'vop_delete',
      operation: 'delete_secret',
      secretName: 'PROTECTED_KEY',
      webauthn: expect.any(Object),
    });
  });
});
