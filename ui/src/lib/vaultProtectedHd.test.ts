import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import wallet from './__fixtures__/protected-hd-public-wallet.json';
import {
  parseProtectedHdReconciliation,
  parseProtectedHdSealResult,
  protectedHdEnvelopeSha256,
  protectedHdMatches,
  protectedHdOperation,
  type ProtectedHdReconciliation,
} from './vaultProtectedHd';

// Read as text: the vectors hold lone surrogates, which the bundler's JSON import refuses.
const fingerprintVectors = JSON.parse(
  readFileSync(resolve(__dirname, '__fixtures__/protected-hd-fingerprint-vectors.json'), 'utf8'),
) as { cases: Array<{ id: string; input: [string, string, string]; sha256: string }> };

const sealed = { envelope: wallet.envelope, accounts: wallet.accounts, expectedAccount0: wallet.expected_account0, establishingVmk: false };
const snapshot = (): ProtectedHdReconciliation => ({
  name: 'COLD_WALLET',
  secret_id: 'vlt_public_1',
  kind: 'keypair',
  protection: 'protected',
  signer_kind: 'hd-seed',
  bitcoin_profile: 'bitcoin-address-types-v1',
  envelope_sha256: wallet.envelope_sha256,
  accounts: structuredClone(wallet.accounts),
  expected_account0: { ...wallet.expected_account0 },
});

describe('protectedHdEnvelopeSha256', () => {
  // The daemon's 18 committed parity vectors (sha256 ec59b255…), including DEL, controls,
  // non-BMP characters and lone surrogates: the parent must hash exactly as the daemon does.
  it.each(fingerprintVectors.cases.map((c) => [c.id, c] as const))('matches the daemon vector %s', (_id, c) => {
    const [ciphertext, nonce, wrapMeta] = c.input;
    expect(protectedHdEnvelopeSha256({ ciphertext, nonce, wrap_meta: wrapMeta })).toBe(c.sha256);
  });

  it('hashes the wrap string as received, never reordered', () => {
    expect(protectedHdEnvelopeSha256(wallet.envelope)).toBe(wallet.envelope_sha256);
    expect(protectedHdEnvelopeSha256({ ...wallet.envelope, wrap_meta: wallet.raw_wrap_reordered })).toBe(wallet.reordered_envelope_sha256);
    expect(wallet.reordered_envelope_sha256).not.toBe(wallet.envelope_sha256);
  });
});

describe('parseProtectedHdSealResult', () => {
  it('takes exactly the four-field result with a 20-row lowercase table', () => {
    expect(parseProtectedHdSealResult(sealed)).toEqual(sealed);
  });

  it.each([
    ['an extra field', { ...sealed, kind: 'sealed-hd-wallet' }],
    ['words', { ...sealed, words: ['abandon'] }],
    ['a short table', { ...sealed, accounts: sealed.accounts.slice(0, 19) }],
    ['rows out of order', { ...sealed, accounts: [sealed.accounts[1], sealed.accounts[0], ...sealed.accounts.slice(2)] }],
    ['uppercase hex', { ...sealed, accounts: sealed.accounts.map((row, i) => (i === 3 ? { ...row, evm: row.evm.toUpperCase() } : row)) }],
    ['a row with an extra key', { ...sealed, accounts: sealed.accounts.map((row, i) => (i === 0 ? { ...row, path: 'x' } : row)) }],
    ['a missing #0 address', { ...sealed, expectedAccount0: { ...sealed.expectedAccount0, btc_legacy: undefined } }],
    ['a non-boolean setup flag', { ...sealed, establishingVmk: 'false' }],
    ['an envelope with an extra key', { ...sealed, envelope: { ...sealed.envelope, record: 'x' } }],
  ])('refuses %s', (_label, value) => {
    expect(parseProtectedHdSealResult(value)).toBeNull();
  });
});

describe('parseProtectedHdReconciliation', () => {
  it('takes exactly the nine-field snapshot', () => {
    expect(parseProtectedHdReconciliation(snapshot())).toEqual(snapshot());
  });

  it.each([
    ['an extra field', { ...snapshot(), secret: {} }],
    ['another profile', { ...snapshot(), bitcoin_profile: 'v2' }],
    ['a standard wallet', { ...snapshot(), protection: 'standard' }],
    ['an empty id', { ...snapshot(), secret_id: '' }],
    ['an uppercase fingerprint', { ...snapshot(), envelope_sha256: wallet.envelope_sha256.toUpperCase() }],
    ['only the enabled rows', { ...snapshot(), accounts: wallet.accounts.slice(0, 1) }],
  ])('refuses %s', (_label, value) => {
    expect(parseProtectedHdReconciliation(value)).toBeNull();
  });
});

describe('protectedHdMatches', () => {
  const operation = protectedHdOperation('COLD_WALLET', sealed);

  it('confirms the exact wallet, and learns no id it was not given', () => {
    expect(protectedHdMatches(snapshot(), operation, null)).toBe(true);
    expect(protectedHdMatches(snapshot(), operation, 'vlt_public_1')).toBe(true);
  });

  it('refuses another record that copies the same material once an id is known', () => {
    expect(protectedHdMatches({ ...snapshot(), secret_id: 'vlt_other' }, operation, 'vlt_public_1')).toBe(false);
  });

  it.each([
    ['another name', (s: ProtectedHdReconciliation) => ({ ...s, name: 'COLD_WALLET_2' })],
    ['another fingerprint', (s: ProtectedHdReconciliation) => ({ ...s, envelope_sha256: wallet.reordered_envelope_sha256 })],
    ['one #0 address in other case', (s: ProtectedHdReconciliation) => ({ ...s, expected_account0: { ...s.expected_account0, eth: s.expected_account0.eth.toLowerCase() } })],
    ['a disabled row that differs', (s: ProtectedHdReconciliation) => ({ ...s, accounts: s.accounts.map((row, i) => (i === 19 ? { ...row, bitcoin_p2wpkh: wallet.accounts[18].bitcoin_p2wpkh } : row)) })],
  ])('refuses %s', (_label, change) => {
    expect(protectedHdMatches(change(snapshot()), operation, null)).toBe(false);
  });
});
