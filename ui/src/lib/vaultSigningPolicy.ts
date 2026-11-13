// Turning ask-before-signing off: the Web side of contract v1.1 (docs/plans/vault-signing-policy.md;
// the frozen wire is _tmp/vault-policy-enable-only/later-policy-contract-v1.1.md). The backend
// issues an exact `context`; the pinned sandbox shows it and returns a WebAuthn assertion over it;
// the backend verifies and applies only the bound true→false change.
import type { VaultWebAuthnSerializedCredential } from '@/context/ApiContext';

/** The sandbox operation, offered only when the pinned build lists it in its capabilities. */
export const POLICY_DOWNGRADE_OP = 'authorizeSigningPolicyDowngrade' as const;

/** Contract §C. The Web never builds or edits one; it passes the server's object through. */
export type PolicyDowngradeContext = {
  v: 1;
  purpose: 'vault-signing-policy-downgrade';
  challengeId: string;
  nonce: string;
  expiresAt: string;
  rpId: string;
  origin: string;
  target: {
    secretId: string;
    name: string;
    protection: 'standard' | 'protected';
    signerKind: 'local' | 'hd-seed';
    account: number | null;
    accountName: string | null;
    addresses: {
      eth: string | null;
      btc_legacy: string | null;
      btc_segwit: string | null;
      btc_taproot: string | null;
      solana: string | null;
    };
    identitySha256: string;
  };
  change: { field: 'always_ask'; from: true; to: false; revision: string };
  factors: Array<{ factorId: string; credentialId: string }>;
};

/** What the sandbox returns: exactly the challenge it answered and the serialized assertion. */
export type PolicyDowngradeResult = {
  challengeId: string;
  assertion: VaultWebAuthnSerializedCredential & {
    type: 'public-key';
    response: { clientDataJSON: string; authenticatorData: string; signature: string; userHandle: string | null };
  };
};

const BASE64URL = /^[A-Za-z0-9_-]+$/;
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const exactKeys = (value: Record<string, unknown>, keys: readonly string[]) => {
  const own = Object.keys(value).sort();
  return own.length === keys.length && [...keys].sort().every((key, index) => key === own[index]);
};

const isBase64 = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && BASE64.test(value);

/**
 * The sandbox's answer, checked before it goes anywhere: exactly `{challengeId, assertion}`, the
 * challenge is the one asked about, and the assertion has the serialized-credential shape and
 * nothing else. Anything else is refused, never repaired.
 */
export function parsePolicyDowngradeResult(value: unknown, context: PolicyDowngradeContext): PolicyDowngradeResult | null {
  if (!isRecord(value) || !exactKeys(value, ['challengeId', 'assertion'])) return null;
  if (value.challengeId !== context.challengeId) return null;
  const assertion = value.assertion;
  if (!isRecord(assertion) || !exactKeys(assertion, ['id', 'rawId', 'type', 'response'])) return null;
  if (assertion.type !== 'public-key') return null;
  if (typeof assertion.id !== 'string' || !BASE64URL.test(assertion.id) || !isBase64(assertion.rawId)) return null;
  const response = assertion.response;
  if (!isRecord(response) || !exactKeys(response, ['clientDataJSON', 'authenticatorData', 'signature', 'userHandle'])) return null;
  if (!isBase64(response.clientDataJSON) || !isBase64(response.authenticatorData) || !isBase64(response.signature)) return null;
  if (response.userHandle !== null && !isBase64(response.userHandle)) return null;
  return value as PolicyDowngradeResult;
}

const decodeBase64 = (value: string): Uint8Array | null => {
  if (!BASE64.test(value)) return null;
  try {
    return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
  } catch {
    return null;
  }
};

const sameBytes = (a: Uint8Array, b: Uint8Array) => a.length === b.length && a.every((byte, index) => byte === b[index]);

/** The context factor whose credential produced this assertion (rawId bytes = credentialId bytes). */
export function factorForAssertion(context: PolicyDowngradeContext, rawId: string): string | null {
  const raw = decodeBase64(rawId);
  if (!raw || raw.length === 0) return null;
  const matches = context.factors.filter((factor) => {
    const credential = decodeBase64(factor.credentialId);
    return credential !== null && sameBytes(credential, raw);
  });
  return matches.length === 1 ? matches[0].factorId : null;
}

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
  vault_policy_authz_used: 'expired',
  vault_policy_state_changed: 'changed',
};

const SANDBOX_REASONS: Record<string, PolicyTurnOffFailure> = {
  policy_downgrade_unavailable: 'unavailable',
  authorization_cancelled: 'cancelled',
  passkey_cancelled: 'cancelled',
  sandbox_reset: 'cancelled',
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
