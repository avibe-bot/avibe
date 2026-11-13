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
