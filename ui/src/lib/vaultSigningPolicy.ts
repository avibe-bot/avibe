// Turning ask-before-signing off: the Web side of contract v1.1 (docs/plans/vault-signing-policy.md;
// the frozen wire is _tmp/vault-policy-enable-only/later-policy-contract-v1.1.md). The backend
// issues an exact `context`; the pinned sandbox shows it and returns a WebAuthn assertion over it;
// the backend verifies and applies only the bound true→false change.
import {
  factorForAssertion,
  type PolicyDowngradeContext,
  type PolicyDowngradeResult,
} from './vaultPolicyWire';

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

export {
  factorForAssertion,
  parsePolicyDowngradeResult,
  POLICY_DOWNGRADE_OP,
  type PolicyDowngradeContext,
  type PolicyDowngradeResult,
} from './vaultPolicyWire';

/** Why turning it off did not happen; the switch stays on in every case. */
export type PolicyTurnOffFailure =
  | 'cancelled' // the user closed the popup or the Passkey prompt: nothing to say
  | 'popup_blocked'
  | 'unavailable' // this sandbox build, or the instance, cannot do it
  | 'setup_required' // no Passkey yet (never set up)
  | 'factor_unavailable' // the Passkey on record is disabled or otherwise unusable for this
  | 'legacy_registration' // set up in an earlier version without the record this check needs
  | 'expired' // the 2-minute confirmation ran out: try again
  | 'clock_skew' // this device's clock is off
  | 'changed' // the key changed elsewhere meanwhile
  | 'failed';

export type PolicyTurnOffOutcome<S> = { ok: true; secret: S } | { ok: false; reason: PolicyTurnOffFailure };

const SERVER_REASONS: Record<string, PolicyTurnOffFailure> = {
  vault_policy_setup_required: 'setup_required',
  vault_policy_legacy_registration_missing: 'legacy_registration',
  vault_policy_factor_unavailable: 'factor_unavailable',
  vault_policy_context_unsupported: 'unavailable',
  vault_policy_downgrade_unavailable: 'unavailable',
  vault_policy_authz_expired: 'expired',
  // Already used (a replay or another tab): not a timeout, so the plain retry line.
  vault_policy_authz_used: 'failed',
  vault_policy_state_changed: 'changed',
};

const SANDBOX_REASONS: Record<string, PolicyTurnOffFailure> = {
  policy_downgrade_unavailable: 'unavailable',
  authorization_cancelled: 'cancelled',
  // The user closed the confirmation window: an ordinary cancel, said nothing about.
  authorization_window_closed: 'cancelled',
  passkey_cancelled: 'cancelled',
  sandbox_reset: 'cancelled',
  authorization_popup_blocked: 'popup_blocked',
  // The window never connected in time: the neutral timed-out line, as for an expired context.
  authorization_window_timeout: 'expired',
  policy_context_expired: 'expired',
  policy_context_clock_skew: 'clock_skew',
};

export const policyFailureFromServer = (code: string | undefined): PolicyTurnOffFailure =>
  (code && SERVER_REASONS[code]) || 'failed';

export const policyFailureFromSandbox = (error: unknown): PolicyTurnOffFailure => {
  const code = isRecord(error) && typeof error.code === 'string' ? error.code : undefined;
  return (code && SANDBOX_REASONS[code]) || 'failed';
};

type PolicyApi<S> = {
  createVaultSigningPolicyChallenge: (name: string, payload: { always_ask: false; account?: number }) => Promise<{ ok: boolean; context?: PolicyDowngradeContext; code?: string }>;
  completeVaultSigningPolicyDowngrade: (
    name: string,
    payload: { context: PolicyDowngradeContext; authorization: { kind: 'webauthn'; challenge_id: string; factor_id: string; assertion: PolicyDowngradeResult['assertion'] } },
  ) => Promise<{ ok: boolean; secret?: S; code?: string }>;
};

type PolicySandbox = {
  authorizeSigningPolicyDowngrade: (payload: { context: PolicyDowngradeContext }, authorizationWindow?: string) => Promise<PolicyDowngradeResult>;
};

/**
 * One attempt at turning ask-before-signing off. The caller opens the authorization window in the
 * click itself (before any await) and closes it afterwards. A fresh challenge every time: a retry
 * is a new call, never a reused assertion. Success only once the server has applied it.
 */
export async function turnOffAskBeforeSigning<S>(
  api: PolicyApi<S>,
  getSandbox: () => Promise<PolicySandbox>,
  target: { name: string; account?: number },
  authorizationWindow: string | undefined,
): Promise<PolicyTurnOffOutcome<S>> {
  // The confirmation lives only in its own top-level window; never ask the sandbox inline instead.
  if (!authorizationWindow) return { ok: false, reason: 'popup_blocked' };
  const challenge = await api.createVaultSigningPolicyChallenge(
    target.name,
    target.account === undefined ? { always_ask: false } : { always_ask: false, account: target.account },
  ).catch(() => ({ ok: false as const, code: undefined }));
  if (!challenge.ok || !challenge.context) return { ok: false, reason: policyFailureFromServer(challenge.code) };
  const context = challenge.context;
  let result: PolicyDowngradeResult;
  try {
    const sandbox = await getSandbox();
    result = await sandbox.authorizeSigningPolicyDowngrade({ context }, authorizationWindow);
  } catch (error) {
    return { ok: false, reason: policyFailureFromSandbox(error) };
  }
  const factorId = factorForAssertion(context, result.assertion.rawId);
  if (!factorId) return { ok: false, reason: 'failed' };
  const done = await api.completeVaultSigningPolicyDowngrade(target.name, {
    context,
    authorization: { kind: 'webauthn', challenge_id: result.challengeId, factor_id: factorId, assertion: result.assertion },
  }).catch(() => ({ ok: false as const, code: undefined, secret: undefined }));
  if (!done.ok || !done.secret) return { ok: false, reason: policyFailureFromServer(done.code) };
  return { ok: true, secret: done.secret };
}
