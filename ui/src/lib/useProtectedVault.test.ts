import { webcrypto } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { vaultFreshSetup, vaultUnlocked, vaultUnlockExpiresAt } from './useProtectedVault';
import { webauthnAvailable } from './vaultEntry';
import {
  VAULT_SANDBOX_EXPECTED_BUILD_HASH,
  VAULT_SANDBOX_IFRAME_URL,
  VAULT_SANDBOX_IFRAME_RESOURCE_PATH,
  VAULT_SANDBOX_ORIGIN,
  VAULT_SANDBOX_PINNED_MANIFEST,
  VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS,
} from './vaultSandboxManifest';

afterEach(() => vi.unstubAllGlobals());

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
    expect(webauthnAvailable()).toBe(false);
  });

  // Parent WebCrypto enables integrity verification. WebAuthn belongs to the
  // HTTPS sandbox, not to the parent's IP/port or navigator.credentials.
  // These capability doubles test the helper, not browser secure-context rules.
  it.each([
    'http://localhost:5123',
    'http://127.0.0.1:5123',
    'http://127.0.0.1:15130',
    'http://[::1]:5123',
    'https://public-fixture.avibe.bot',
  ])('allows a WebCrypto-capable parent at %s without parent-origin WebAuthn', (origin) => {
    vi.stubGlobal('window', { location: new URL(origin) });
    vi.stubGlobal('crypto', webcrypto);
    vi.stubGlobal('navigator', undefined);
    expect(webauthnAvailable()).toBe(true);
  });

  it.each([
    { origin: 'http://192.168.2.3:5123', crypto: { subtle: undefined } },
    { origin: 'http://127.0.0.1:5123', crypto: undefined },
    { origin: 'https://public-fixture.avibe.bot', crypto: { subtle: undefined } },
  ])('fails closed without parent WebCrypto at $origin', ({ origin, crypto }) => {
    vi.stubGlobal('window', { location: new URL(origin) });
    vi.stubGlobal('crypto', crypto);
    expect(webauthnAvailable()).toBe(false);
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
