import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The policy module fetches `/api/vault/settings` via apiFetch; mock it so the fail-closed path is
// deterministic (the fetch outcome is controlled per test rather than depending on the environment).
vi.mock('./apiFetch', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './apiFetch';
import {
  DEFAULT_VAULT_SESSION_POLICY,
  STRICT_FALLBACK_VAULT_SESSION_POLICY,
  getVaultSandboxPolicy,
  normalizeVaultSessionPolicy,
  refreshVaultSandboxPolicy,
  resetVaultSandboxPolicyForTests,
  setVaultSandboxPolicy,
} from './vaultSandboxPolicy';

const mockedApiFetch = vi.mocked(apiFetch);

beforeEach(() => {
  resetVaultSandboxPolicyForTests();
  mockedApiFetch.mockReset();
});

afterEach(() => {
  resetVaultSandboxPolicyForTests();
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

describe('refreshVaultSandboxPolicy — fail closed', () => {
  it('adopts the daemon policy on a successful fetch', async () => {
    mockedApiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ policy: { windowSeconds: 1800, strictApprovals: true, parentValueSealAllowed: true } }),
    } as unknown as Response);
    const policy = await refreshVaultSandboxPolicy();
    expect(policy).toEqual({ windowSeconds: 1800, strictApprovals: true, parentValueSealAllowed: true });
  });

  it('fails closed to the strict fallback when the fetch fails before any confirmation', async () => {
    mockedApiFetch.mockRejectedValue(new Error('offline'));
    const policy = await refreshVaultSandboxPolicy();
    // Not the permissive default — a fetch failure on a fresh tab must not relax Strict/window.
    expect(policy).toEqual(STRICT_FALLBACK_VAULT_SESSION_POLICY);
    expect(policy.strictApprovals).toBe(true);
    expect(policy.windowSeconds).toBe(300);
  });

  it('fails closed on a non-ok response too', async () => {
    mockedApiFetch.mockResolvedValue({ ok: false, json: async () => ({}) } as unknown as Response);
    expect(await refreshVaultSandboxPolicy()).toEqual(STRICT_FALLBACK_VAULT_SESSION_POLICY);
  });

  it('keeps a previously-confirmed policy on a later fetch failure (no over-restriction)', async () => {
    // A prior successful set confirms the real (relaxed) policy...
    setVaultSandboxPolicy({ windowSeconds: 1800, strictApprovals: false, parentValueSealAllowed: true });
    mockedApiFetch.mockRejectedValue(new Error('offline'));
    const policy = await refreshVaultSandboxPolicy();
    // ...so a transient failure keeps it instead of forcing the strict fallback.
    expect(policy.windowSeconds).toBe(1800);
    expect(policy.strictApprovals).toBe(false);
  });
});
