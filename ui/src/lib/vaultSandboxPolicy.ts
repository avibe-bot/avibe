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

/**
 * Whether a value is a *complete, well-formed* daemon policy — every field present with a valid
 * value. This is stricter than "is an object": it must be validated before confirming so a
 * malformed HTTP-200 payload (missing/misnamed fields during a skewed deploy) can't slip through
 * `normalizeVaultSessionPolicy`, which would silently default the missing security fields to the
 * relaxed values (non-strict / 10 min). A malformed policy must fail closed, not be trusted.
 */
export function isValidVaultSessionPolicyShape(value: unknown): boolean {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    (record.windowSeconds === 300 || record.windowSeconds === 600 || record.windowSeconds === 1800) &&
    typeof record.strictApprovals === 'boolean' &&
    typeof record.parentValueSealAllowed === 'boolean'
  );
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
// Whether the mirror has ever been confirmed from the daemon (a successful GET, or an explicit set
// from the settings dialog). Until it has, a settings-fetch failure fails closed rather than trust
// the permissive default (see refreshVaultSandboxPolicy).
let policyConfirmed = false;

/**
 * The strictest posture, used as a fail-closed fallback when the daemon settings can't be fetched
 * before the real policy has ever been confirmed: shortest window + a passkey on every
 * approval/reveal. This ensures a settings-fetch failure on a fresh tab can never silently relax a
 * configured Strict/short-window policy down to the permissive default. `parentValueSealAllowed`
 * stays true — it gates protected static *creation* (#842), not approval posture, so failing it
 * closed would only block creation without tightening any authorization.
 */
export const STRICT_FALLBACK_VAULT_SESSION_POLICY: VaultSessionPolicy = {
  windowSeconds: 300,
  strictApprovals: true,
  parentValueSealAllowed: true,
};

export function getVaultSandboxPolicy(): VaultSessionPolicy {
  return { ...currentPolicy };
}

export function setVaultSandboxPolicy(value: unknown): VaultSessionPolicy {
  currentPolicy = normalizeVaultSessionPolicy(value);
  policyConfirmed = true;
  return getVaultSandboxPolicy();
}

// On a settings-fetch failure: keep a previously-confirmed policy (we already know the user's real
// settings, so don't over-restrict on a transient blip), but if the real policy has never been
// confirmed, assume the strict fallback instead of the permissive default so the failure can't
// quietly weaken Strict/window on a fresh tab.
function failClosedVaultSandboxPolicy(): VaultSessionPolicy {
  if (!policyConfirmed) currentPolicy = { ...STRICT_FALLBACK_VAULT_SESSION_POLICY };
  return getVaultSandboxPolicy();
}

/**
 * Refresh the parent's policy mirror from the daemon (`GET /api/vault/settings`). Called by the
 * sandbox client at handshake/unlock/setup so the ceremony runs under the configured window/strict
 * values. On failure it fails **closed** (strict fallback) until a real policy is confirmed, then
 * keeps the last-confirmed policy — a fetch failure must never relax the configured posture.
 */
export async function refreshVaultSandboxPolicy(): Promise<VaultSessionPolicy> {
  try {
    const res = await apiFetch('/api/vault/settings');
    if (!res.ok) return failClosedVaultSandboxPolicy();
    const body = (await res.json()) as { ok?: unknown; policy?: unknown };
    // Require a genuine success payload carrying a *complete, well-formed* policy before trusting
    // it. An application-level failure or a malformed policy returned as HTTP 200 (ok:false, or a
    // policy object missing/misnaming fields) must fail closed — not confirm a default via
    // normalize(), which would silently relax a configured Strict/short-window posture.
    if (body?.ok === false || !isValidVaultSessionPolicyShape(body?.policy)) {
      return failClosedVaultSandboxPolicy();
    }
    return setVaultSandboxPolicy(body.policy);
  } catch {
    return failClosedVaultSandboxPolicy();
  }
}

/**
 * Invalidate the mirror after a policy reset (a tab enabled Strict / shortened the window). Drops
 * the confirmed flag and falls back to the strict posture so that if the next handshake's settings
 * fetch fails, we don't re-pin the old *confirmed* (relaxed) policy — the reset was a tightening,
 * so failing closed is correct. A successful refresh at the next handshake replaces this.
 */
export function invalidateVaultSandboxPolicy(): void {
  currentPolicy = { ...STRICT_FALLBACK_VAULT_SESSION_POLICY };
  policyConfirmed = false;
}

/** Reset the shared mirror for tests (both the value and the confirmed flag). */
export function resetVaultSandboxPolicyForTests(): void {
  currentPolicy = { ...DEFAULT_VAULT_SESSION_POLICY };
  policyConfirmed = false;
}
