import { describe, expect, it } from 'vitest';

import { vaultFreshSetup, vaultUnlocked, vaultUnlockExpiresAt } from './useProtectedVault';
import { vaultEntryIssue } from './vaultEntry';
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
    expect(VAULT_SANDBOX_EXPECTED_BUILD_HASH).toMatch(/^[a-f0-9]{64}$/);
    expect(VAULT_SANDBOX_IFRAME_RESOURCE_PATH).toBe(`/v/${VAULT_SANDBOX_EXPECTED_BUILD_HASH}/index.html`);
    expect(VAULT_SANDBOX_IFRAME_URL).toBe(`${VAULT_SANDBOX_ORIGIN}${VAULT_SANDBOX_IFRAME_RESOURCE_PATH}`);
    expect((VAULT_SANDBOX_PINNED_MANIFEST.resources as Record<string, string>)[VAULT_SANDBOX_IFRAME_RESOURCE_PATH]).toMatch(/^sha256-/);
    expect(VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS).toContain(VAULT_SANDBOX_IFRAME_RESOURCE_PATH);
    expect(VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS.every((path) => !path.endsWith('.map'))).toBe(true);
  });

  it('fails closed outside a browser context', () => {
    expect(vaultEntryIssue()).toBe('browser');
  });
});

describe('event-driven lock state (parent shadow clock removed)', () => {
  it('starts locked with no mirrored expiry — nothing unlocks without a sandbox vault.state event', () => {
    // The parent no longer owns an auto-lock timer or a default window; lock state is derived only
    // from sandbox events and status results (protocol v2 §8), so the module boots fully locked.
    expect(vaultUnlocked()).toBe(false);
    expect(vaultUnlockExpiresAt()).toBeNull();
    expect(vaultFreshSetup()).toBe(false);
  });
});
