import { describe, expect, it } from 'vitest';

import { webauthnAvailable } from './useProtectedVault';
import {
  VAULT_SANDBOX_EXPECTED_BUILD_HASH,
  VAULT_SANDBOX_IFRAME_URL,
  VAULT_SANDBOX_ORIGIN,
  VAULT_SANDBOX_PINNED_MANIFEST,
  VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS,
} from './vaultSandboxManifest';

describe('protected vault sandbox cutover', () => {
  it('pins the deployed sandbox build and only verifies runtime resources', () => {
    expect(VAULT_SANDBOX_ORIGIN).toBe('https://sandbox.avibe.bot');
    expect(VAULT_SANDBOX_EXPECTED_BUILD_HASH).toBe('dev');
    expect(VAULT_SANDBOX_IFRAME_URL.startsWith(`${VAULT_SANDBOX_ORIGIN}/index.html?`)).toBe(true);
    expect(VAULT_SANDBOX_PINNED_MANIFEST.resources['/index.html']).toMatch(/^sha256-/);
    expect(VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS).toContain('/index.html');
    expect(VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS.every((path) => !path.endsWith('.map'))).toBe(true);
  });

  it('fails closed outside a browser context', () => {
    expect(webauthnAvailable()).toBe(false);
  });
});
