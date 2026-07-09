import { afterEach, describe, expect, it } from 'vitest';

import {
  DEFAULT_VAULT_SESSION_POLICY,
  getVaultSandboxPolicy,
  normalizeVaultSessionPolicy,
  setVaultSandboxPolicy,
} from './vaultSandboxPolicy';

afterEach(() => {
  // The module holds a shared mirror; reset it so tests don't leak policy into each other.
  setVaultSandboxPolicy(DEFAULT_VAULT_SESSION_POLICY);
});

describe('normalizeVaultSessionPolicy', () => {
  it('defaults a non-object to the safe default policy', () => {
    expect(normalizeVaultSessionPolicy(undefined)).toEqual(DEFAULT_VAULT_SESSION_POLICY);
    expect(normalizeVaultSessionPolicy(null)).toEqual(DEFAULT_VAULT_SESSION_POLICY);
    expect(normalizeVaultSessionPolicy('nope')).toEqual(DEFAULT_VAULT_SESSION_POLICY);
  });

  it('accepts only the fixed unlock-window options, else falls back to 10 min', () => {
    expect(normalizeVaultSessionPolicy({ windowSeconds: 300 }).windowSeconds).toBe(300);
    expect(normalizeVaultSessionPolicy({ windowSeconds: 600 }).windowSeconds).toBe(600);
    expect(normalizeVaultSessionPolicy({ windowSeconds: 1800 }).windowSeconds).toBe(1800);
    // Off-menu values (incl. the old 3600 option) normalize to the default.
    expect(normalizeVaultSessionPolicy({ windowSeconds: 3600 }).windowSeconds).toBe(600);
    expect(normalizeVaultSessionPolicy({ windowSeconds: 'x' }).windowSeconds).toBe(600);
  });

  it('is default-safe for the booleans — only an explicit flag weakens posture', () => {
    // strictApprovals only relaxes to true on an explicit true.
    expect(normalizeVaultSessionPolicy({ strictApprovals: true }).strictApprovals).toBe(true);
    expect(normalizeVaultSessionPolicy({ strictApprovals: 'yes' }).strictApprovals).toBe(false);
    expect(normalizeVaultSessionPolicy({}).strictApprovals).toBe(false);
    // parentValueSealAllowed only disables on an explicit false.
    expect(normalizeVaultSessionPolicy({ parentValueSealAllowed: false }).parentValueSealAllowed).toBe(false);
    expect(normalizeVaultSessionPolicy({ parentValueSealAllowed: 0 }).parentValueSealAllowed).toBe(true);
    expect(normalizeVaultSessionPolicy({}).parentValueSealAllowed).toBe(true);
  });
});

describe('policy mirror', () => {
  it('round-trips a set policy and hands back copies (no shared reference)', () => {
    const stored = setVaultSandboxPolicy({ windowSeconds: 1800, strictApprovals: true, parentValueSealAllowed: false });
    expect(stored).toEqual({ windowSeconds: 1800, strictApprovals: true, parentValueSealAllowed: false });
    const a = getVaultSandboxPolicy();
    const b = getVaultSandboxPolicy();
    expect(a).toEqual(stored);
    expect(a).not.toBe(b);
    a.strictApprovals = false;
    expect(getVaultSandboxPolicy().strictApprovals).toBe(true);
  });
});
