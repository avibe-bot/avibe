#!/usr/bin/env node
// PUBLIC NEVER-FUND contract probes only. Node built-ins, no product imports.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createCipheriv, createDecipheriv, createHash, pbkdf2Sync, ECDH } from 'node:crypto';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixture = JSON.parse(fs.readFileSync(path.join(root, 'protected-hd-codec-vectors.json'), 'utf8'));
const fingerprintCases = JSON.parse(fs.readFileSync(path.join(root, 'protected-hd-fingerprint-vectors.json'), 'utf8')).cases;
const hash = value => createHash('sha256').update(value).digest('hex');
const stable = value => value === null || typeof value !== 'object' ? JSON.stringify(value)
  : Array.isArray(value) ? `[${value.map(stable).join(',')}]`
  : `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${stable(value[k])}`).join(',')}}`;
const fingerprintText = triple => JSON.stringify(triple).replace(/[\u007f-\uffff]/g,
  c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
const fingerprint = envelope => hash(fingerprintText(['ciphertext', 'nonce', 'wrap_meta'].map(k => envelope[k])));
const hex = value => Buffer.from(value, 'hex');
const hash160 = value => createHash('ripemd160').update(createHash('sha256').update(value).digest()).digest();
function base58check(bytes) {
  const checksum = createHash('sha256').update(createHash('sha256').update(bytes).digest()).digest().subarray(0, 4);
  const payload = Buffer.concat([bytes, checksum]);
  let value = BigInt('0x' + payload.toString('hex'));
  const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  let out = '';
  while (value) { out = alphabet[Number(value % 58n)] + out; value /= 58n; }
  for (const byte of payload) { if (byte !== 0) break; out = '1' + out; }
  return out;
}
function p2wpkh(publicKey) {
  const program = hash160(publicKey), data = [0];
  let accumulator = 0, bits = 0;
  for (const byte of program) {
    accumulator = ((accumulator << 8) | byte) & 0xffff;
    bits += 8;
    while (bits >= 5) { bits -= 5; data.push((accumulator >>> bits) & 31); }
  }
  if (bits) data.push((accumulator << (5 - bits)) & 31);
  const expanded = [3, 3, 0, 2, 3]; // HRP expansion of 'bc'.
  let polymod = 1;
  for (const value of [...expanded, ...data, 0, 0, 0, 0, 0, 0]) {
    const top = polymod >>> 25;
    polymod = (((polymod & 0x1ffffff) << 5) ^ value) >>> 0;
    for (const [i, generator] of [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3].entries()) {
      if ((top >>> i) & 1) polymod = (polymod ^ generator) >>> 0;
    }
  }
  polymod = (polymod ^ 1) >>> 0; // Bech32 v0, NOT Bech32m.
  const checksum = Array.from({ length: 6 }, (_, i) => (polymod >>> (5 * (5 - i))) & 31);
  const alphabet = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l';
  return 'bc1' + [...data, ...checksum].map(value => alphabet[value]).join('');
}
const base64 = value => {
  const bytes = Buffer.from(value, 'base64');
  assert.equal(bytes.toString('base64'), value);
  return bytes;
};
const marker = Buffer.from('avibe:protected-hd-wallet:v1\n');
const aadObject = c => ({ domain: 'avault:protected-record:aad:v2', name: c.name,
  record: c.record_meta, scheme: 'machine-aesgcm-v1', version: 1 });
function decrypt(key, nonce, data, aad) {
  const decipher = createDecipheriv('aes-256-gcm', key, nonce);
  decipher.setAAD(aad);
  decipher.setAuthTag(data.subarray(-16));
  return Buffer.concat([decipher.update(data.subarray(0, -16)), decipher.final()]);
}
function encrypt(key, nonce, plaintext, aad) {
  const cipher = createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(aad);
  return Buffer.concat([cipher.update(plaintext), cipher.final(), cipher.getAuthTag()]);
}
function payload(bytes) {
  assert.deepEqual(bytes.subarray(0, 29), marker);
  const length = bytes[29];
  assert([16, 20, 24, 28, 32].includes(length));
  assert.equal(bytes.length, 95 + length);
  assert([0, 1].includes(bytes.at(-1)));
  return { entropy: bytes.subarray(30, 30 + length), seed: bytes.subarray(30 + length, 94 + length), flag: bytes.at(-1) };
}
for (const c of fingerprintCases) {
  const text = fingerprintText(c.input);
  assert.equal(text, c.serialized, c.id);
  assert.equal(Buffer.from(text).toString('hex'), c.utf8_hex, c.id);
  assert.equal(hash(text), c.sha256, c.id);
}
assert.equal(fixture.profile, 'bitcoin-address-types-v1');
assert.deepEqual(fixture.bitcoin_address_types, { p2pkh: 'bitcoin_p2pkh', p2wpkh: 'bitcoin_p2wpkh', p2tr: 'bitcoin' });
for (const vector of fixture.official_bip84_receiving) {
  assert.equal(p2wpkh(hex(vector.public_key)), vector.address);
}
let aadTamperFailures = 0;
for (const c of fixture.cases) {
  assert.equal(c.record_meta.profile, fixture.profile);
  const aad = Buffer.from(stable(aadObject(c)));
  assert.equal(aad.toString(), c.aad);
  assert.equal(aad.toString('hex'), c.aad_hex);
  const parsed = payload(hex(c.payload_hex));
  assert.equal(parsed.entropy.toString('hex'), c.entropy_hex);
  assert.equal(parsed.seed.toString('hex'), c.seed_hex);
  assert.equal(parsed.flag, c.passphrase_required);
  const seed = pbkdf2Sync(c.words.normalize('NFKD'), 'mnemonic' + c.passphrase.normalize('NFKD'), 2048, 64, 'sha512');
  assert.equal(seed.toString('hex'), c.seed_hex);
  assert.equal(Number(c.passphrase.normalize('NFKD').length > 0), c.passphrase_required);
  const wrap = JSON.parse(c.envelope.wrap_meta);
  assert.deepEqual(wrap.record_meta, c.record_meta);
  assert.equal(c.record_meta.accounts.length, 20);
  for (const [i, row] of c.record_meta.accounts.entries()) {
    assert.deepEqual(Object.keys(row).sort(), ['account', 'bitcoin', 'bitcoin_p2pkh', 'bitcoin_p2wpkh', 'evm', 'solana']);
    assert.equal(row.account, i);
    for (const chain of ['evm', 'bitcoin', 'bitcoin_p2pkh', 'bitcoin_p2wpkh']) {
      assert.equal(row[chain].length, 66);
      assert.equal(ECDH.convertKey(hex(row[chain]), 'secp256k1', undefined, 'hex', 'compressed'), row[chain]);
    }
    assert.equal(hex(row.solana).length, 32);
  }
  assert.deepEqual(Object.keys(c.expected_account0).sort(), ['btc_legacy', 'btc_segwit', 'btc_taproot', 'eth', 'solana']);
  assert.equal(base58check(Buffer.concat([Buffer.from([0]), hash160(hex(c.record_meta.accounts[0].bitcoin_p2pkh))])), c.expected_account0.btc_legacy);
  assert.equal(p2wpkh(hex(c.record_meta.accounts[0].bitcoin_p2wpkh)), c.expected_account0.btc_segwit);
  const vmk = hex(c.vmk_hex), dek = hex(c.dek_hex), nonce = base64(c.envelope.nonce);
  const wrapped = base64(wrap.wrapped_dek), ciphertext = base64(c.envelope.ciphertext), dekNonce = base64(wrap.dek_nonce);
  assert.equal(nonce.length, 12); assert.equal(dekNonce.length, 12); assert.equal(wrapped.length, 48);
  assert.equal(ciphertext.length, c.ciphertext_bytes);
  assert.deepEqual(encrypt(vmk, dekNonce, dek, aad), wrapped);
  assert.deepEqual(encrypt(dek, nonce, hex(c.payload_hex), aad), ciphertext);
  assert.deepEqual(decrypt(vmk, dekNonce, wrapped, aad), dek);
  assert.equal(decrypt(dek, nonce, ciphertext, aad).toString('hex'), c.payload_hex);
  assert.equal(fingerprint(c.envelope), c.envelope_sha256);
  assert.equal(fingerprint({ ...c.envelope, wrap_meta: c.raw_wrap_reordered }), c.reordered_envelope_sha256);
  assert.notEqual(c.envelope_sha256, c.reordered_envelope_sha256);
  for (const negative of fixture.negative_cases.filter(n => n.base === c.id && n.reject_at === 'wrapped-dek-and-payload-aead')) {
    const mutated = structuredClone(aadObject(c));
    if (negative.replace_name) mutated.name = negative.replace_name;
    if (negative.replace_record_account0_evm) mutated.record.accounts[0].evm = negative.replace_record_account0_evm;
    if (negative.replace_record_account0_field) Object.assign(mutated.record.accounts[0], negative.replace_record_account0_field);
    const changedAad = Buffer.from(stable(mutated));
    assert.throws(() => decrypt(vmk, dekNonce, wrapped, changedAad));
    assert.throws(() => decrypt(dek, nonce, ciphertext, changedAad));
    aadTamperFailures += 2;
  }
}
let codecRefusals = 0;
for (const negative of fixture.negative_cases) {
  if (negative.reject_at === 'payload-codec') {
    assert.throws(() => payload(hex(negative.payload_hex)), negative.id);
    codecRefusals++;
  } else if (negative.id === 'wrong-entropy-right-seed') {
    const wrong = payload(hex(negative.payload_hex));
    const original = fixture.cases.find(c => c.id === negative.base);
    assert.equal(wrong.seed.toString('hex'), original.seed_hex);
    assert.notEqual(wrong.entropy.toString('hex'), original.entropy_hex);
  }
}
for (const c of fixture.legacy_aad) {
  const text = stable({ domain: 'avault:protected-record:aad:v2', name: c.name, record: c.record,
    scheme: 'machine-aesgcm-v1', version: 1 });
  assert.equal(text, c.aad);
  assert.equal(Buffer.from(text).toString('hex'), c.aad_hex);
}
console.log(JSON.stringify({ scope: 'public reference contract probes, not product acceptance', fingerprint_vectors: fingerprintCases.length,
  payload_aad_aead_vectors: fixture.cases.length, legacy_aad_vectors: fixture.legacy_aad.length,
  payload_refusals: codecRefusals, aead_tamper_refusals: aadTamperFailures,
  official_bip84_address_encodings: fixture.official_bip84_receiving.length,
  three_btc_account0_address_encodings: fixture.cases.length * 2,
  derivation_oracle: 'Python generator cross-checks bip_utils+embit, not this Node probe',
  node: process.version }));
