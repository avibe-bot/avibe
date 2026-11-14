// Protected seed-phrase wallets (docs/plans/vault-protected-hd-wallets.md, revision 0.7): the
// parent-side half. The sandbox window makes or takes the words and returns only ciphertext and
// public keys; this module checks that result's shape, fingerprints its envelope exactly as the
// daemon does, and decides whether what the daemon saved is the wallet that window made. Nothing
// here ever sees words, a passphrase or a seed.
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex } from '@noble/hashes/utils.js';

export const PROTECTED_HD_ACCOUNT_COUNT = 20;
const HEX66 = /^0[23][0-9a-f]{64}$/;
const HEX64 = /^[0-9a-f]{64}$/;

export type ProtectedHdEnvelope = { ciphertext: string; nonce: string; wrap_meta: string };

/** One immutable row of the public table: compressed secp256k1 points and an Ed25519 key, all lowercase hex. */
export type ProtectedHdAccountRow = {
  account: number;
  evm: string;
  bitcoin: string;
  bitcoin_p2pkh: string;
  bitcoin_p2wpkh: string;
  solana: string;
};

export type ProtectedHdAccount0 = {
  eth: string;
  btc_legacy: string;
  btc_segwit: string;
  btc_taproot: string;
  solana: string;
};

/** The ordinary sandbox RPC result of `sealHdWallet`: exactly these four fields. */
export type ProtectedHdSealResult = {
  envelope: ProtectedHdEnvelope;
  accounts: ProtectedHdAccountRow[];
  expectedAccount0: ProtectedHdAccount0;
  establishingVmk: boolean;
};

/** The daemon's value-free identity snapshot (POST success and the exact-name GET). */
export type ProtectedHdReconciliation = {
  name: string;
  secret_id: string;
  kind: 'keypair';
  protection: 'protected';
  signer_kind: 'hd-seed';
  bitcoin_profile: 'bitcoin-address-types-v1';
  envelope_sha256: string;
  accounts: ProtectedHdAccountRow[];
  expected_account0: ProtectedHdAccount0;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const exactKeys = (value: Record<string, unknown>, keys: readonly string[]) => {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => Object.hasOwn(value, key));
};

const ROW_KEYS = ['account', 'evm', 'bitcoin', 'bitcoin_p2pkh', 'bitcoin_p2wpkh', 'solana'] as const;
const ACCOUNT0_KEYS = ['eth', 'btc_legacy', 'btc_segwit', 'btc_taproot', 'solana'] as const;

function parseRows(value: unknown): ProtectedHdAccountRow[] | null {
  if (!Array.isArray(value) || value.length !== PROTECTED_HD_ACCOUNT_COUNT) return null;
  const rows: ProtectedHdAccountRow[] = [];
  for (const [index, row] of value.entries()) {
    if (!isRecord(row) || !exactKeys(row, ROW_KEYS)) return null;
    if (row.account !== index || !Number.isInteger(row.account)) return null;
    for (const key of ['evm', 'bitcoin', 'bitcoin_p2pkh', 'bitcoin_p2wpkh'] as const) {
      if (typeof row[key] !== 'string' || !HEX66.test(row[key])) return null;
    }
    if (typeof row.solana !== 'string' || !HEX64.test(row.solana)) return null;
    rows.push({
      account: index,
      evm: row.evm as string,
      bitcoin: row.bitcoin as string,
      bitcoin_p2pkh: row.bitcoin_p2pkh as string,
      bitcoin_p2wpkh: row.bitcoin_p2wpkh as string,
      solana: row.solana,
    });
  }
  return rows;
}

function parseAccount0(value: unknown): ProtectedHdAccount0 | null {
  if (!isRecord(value) || !exactKeys(value, ACCOUNT0_KEYS)) return null;
  if (!ACCOUNT0_KEYS.every((key) => typeof value[key] === 'string' && (value[key] as string).length > 0)) return null;
  return {
    eth: value.eth as string,
    btc_legacy: value.btc_legacy as string,
    btc_segwit: value.btc_segwit as string,
    btc_taproot: value.btc_taproot as string,
    solana: value.solana as string,
  };
}

function parseEnvelope(value: unknown): ProtectedHdEnvelope | null {
  if (!isRecord(value) || !exactKeys(value, ['ciphertext', 'nonce', 'wrap_meta'])) return null;
  const { ciphertext, nonce, wrap_meta: wrapMeta } = value;
  if (typeof ciphertext !== 'string' || typeof nonce !== 'string' || typeof wrapMeta !== 'string') return null;
  if (!ciphertext || !nonce || !wrapMeta) return null;
  return { ciphertext, nonce, wrap_meta: wrapMeta };
}

/** The sandbox's `sealHdWallet` result, or null for anything else (extra fields, a short table, uppercase hex…). */
export function parseProtectedHdSealResult(value: unknown): ProtectedHdSealResult | null {
  if (!isRecord(value) || !exactKeys(value, ['envelope', 'accounts', 'expectedAccount0', 'establishingVmk'])) return null;
  const envelope = parseEnvelope(value.envelope);
  const accounts = parseRows(value.accounts);
  const expectedAccount0 = parseAccount0(value.expectedAccount0);
  if (!envelope || !accounts || !expectedAccount0 || typeof value.establishingVmk !== 'boolean') return null;
  return { envelope, accounts, expectedAccount0, establishingVmk: value.establishingVmk };
}

/** The daemon's reconciliation snapshot, or null when anything is missing, extra or malformed. */
export function parseProtectedHdReconciliation(value: unknown): ProtectedHdReconciliation | null {
  if (!isRecord(value)) return null;
  const keys = ['name', 'secret_id', 'kind', 'protection', 'signer_kind', 'bitcoin_profile', 'envelope_sha256', 'accounts', 'expected_account0'];
  if (!exactKeys(value, keys)) return null;
  if (typeof value.name !== 'string' || typeof value.secret_id !== 'string' || !value.secret_id) return null;
  if (value.kind !== 'keypair' || value.protection !== 'protected' || value.signer_kind !== 'hd-seed') return null;
  if (value.bitcoin_profile !== 'bitcoin-address-types-v1') return null;
  if (typeof value.envelope_sha256 !== 'string' || !HEX64.test(value.envelope_sha256)) return null;
  const accounts = parseRows(value.accounts);
  const account0 = parseAccount0(value.expected_account0);
  if (!accounts || !account0) return null;
  return {
    name: value.name,
    secret_id: value.secret_id,
    kind: 'keypair',
    protection: 'protected',
    signer_kind: 'hd-seed',
    bitcoin_profile: 'bitcoin-address-types-v1',
    envelope_sha256: value.envelope_sha256,
    accounts,
    expected_account0: account0,
  };
}

/**
 * The daemon's `hd_signing_binding` fingerprint, byte for byte: SHA-256 of Python's
 * `json.dumps([ciphertext, nonce, wrap_meta], separators=(",", ":"), ensure_ascii=True)`. The wrap
 * string is hashed as received, never parsed or reordered. JSON.stringify matches Python except that
 * Python also escapes DEL and every non-ASCII code unit (non-BMP characters as two surrogates).
 */
export function protectedHdEnvelopeSha256(envelope: ProtectedHdEnvelope): string {
  const serialized = JSON.stringify([envelope.ciphertext, envelope.nonce, envelope.wrap_meta])
    .replace(/[\u007f-\uffff]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
  return bytesToHex(sha256(new TextEncoder().encode(serialized)));
}

/** What this browser holds from the window it opened, kept until the save is confirmed. */
export type ProtectedHdOperation = {
  name: string;
  envelopeSha256: string;
  accounts: ProtectedHdAccountRow[];
  expectedAccount0: ProtectedHdAccount0;
};

export function protectedHdOperation(name: string, sealed: ProtectedHdSealResult): ProtectedHdOperation {
  return {
    name,
    envelopeSha256: protectedHdEnvelopeSha256(sealed.envelope),
    accounts: sealed.accounts,
    expectedAccount0: sealed.expectedAccount0,
  };
}

/**
 * Whether a snapshot is exactly the wallet this browser's window made: the same name and
 * classification, every field of all 20 rows, the envelope fingerprint and all five #0 strings
 * (exact, case-sensitive). Once a record ID has been seen, it must be that same ID even if another
 * record carries copies of the same material.
 */
export function protectedHdMatches(
  snapshot: ProtectedHdReconciliation,
  operation: ProtectedHdOperation,
  knownSecretId: string | null,
): boolean {
  if (knownSecretId !== null && snapshot.secret_id !== knownSecretId) return false;
  if (snapshot.name !== operation.name || snapshot.envelope_sha256 !== operation.envelopeSha256) return false;
  for (const key of ACCOUNT0_KEYS) {
    if (snapshot.expected_account0[key] !== operation.expectedAccount0[key]) return false;
  }
  return snapshot.accounts.every((row, index) => {
    const mine = operation.accounts[index];
    return ROW_KEYS.every((key) => row[key] === mine[key]);
  });
}
