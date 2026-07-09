import { apiFetch } from './apiFetch';

/**
 * The vault session policy the daemon persists and the sandbox enforces (protocol v2 §6.5).
 *
 * This parent-held copy is **display-only**: it is handed to the sandbox at `handshake` and
 * `unlock` so the sandbox knows the unlock-window length and whether Strict approvals is on. The
 * sandbox is the sole enforcer — never gate a real security decision on this mirror.
 */
export type VaultSessionPolicy = {
  /** Unlock-window length. One of the daemon's fixed options (5 / 10 / 30 min). */
  windowSeconds: 300 | 600 | 1800;
  /** When true, R2 (approve/reveal) behaves like R3 — a passkey every time. */
  strictApprovals: boolean;
  /** The #842 concession switch: whether newly-typed static values may be sealed via parent value. */
  parentValueSealAllowed: boolean;
};

export const DEFAULT_VAULT_SESSION_POLICY: VaultSessionPolicy = {
  windowSeconds: 600,
  strictApprovals: false,
  parentValueSealAllowed: true,
};

function normalizeWindowSeconds(value: unknown): 300 | 600 | 1800 {
  return value === 300 || value === 600 || value === 1800 ? value : DEFAULT_VAULT_SESSION_POLICY.windowSeconds;
}

/** Coerce any daemon/settings-shaped value into a complete, safe policy object. */
export function normalizeVaultSessionPolicy(value: unknown): VaultSessionPolicy {
  if (typeof value !== 'object' || value == null) return { ...DEFAULT_VAULT_SESSION_POLICY };
  const record = value as Record<string, unknown>;
  return {
    windowSeconds: normalizeWindowSeconds(record.windowSeconds),
    // Default-safe: only an explicit `true` relaxes to Strict / only an explicit `false` disables
    // parent-value sealing, so a malformed field can never silently weaken the posture.
    strictApprovals: record.strictApprovals === true,
    parentValueSealAllowed: record.parentValueSealAllowed !== false,
  };
}

// Module-level mirror so the (non-React) sandbox client can read the latest policy at handshake
// and unlock without threading it through every call site. The settings UI updates it after a
// successful PATCH so the next unlock already runs under the new window/strict values.
let currentPolicy: VaultSessionPolicy = { ...DEFAULT_VAULT_SESSION_POLICY };

export function getVaultSandboxPolicy(): VaultSessionPolicy {
  return { ...currentPolicy };
}

export function setVaultSandboxPolicy(value: unknown): VaultSessionPolicy {
  currentPolicy = normalizeVaultSessionPolicy(value);
  return getVaultSandboxPolicy();
}

/**
 * Best-effort refresh of the parent's policy mirror from the daemon (`GET /api/vault/settings`).
 * Called by the sandbox client at handshake so the very first ceremony already runs under the
 * configured window/strict values. A failure keeps the last-known (or default) policy — the
 * sandbox enforces its own copy regardless, so this is only about display + unlock hints.
 */
export async function refreshVaultSandboxPolicy(): Promise<VaultSessionPolicy> {
  try {
    const res = await apiFetch('/api/vault/settings');
    if (!res.ok) return getVaultSandboxPolicy();
    const body = (await res.json()) as { policy?: unknown };
    return setVaultSandboxPolicy(body.policy);
  } catch {
    return getVaultSandboxPolicy();
  }
}
