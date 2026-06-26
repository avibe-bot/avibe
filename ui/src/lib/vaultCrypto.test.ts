import { describe, expect, it } from 'vitest';
import { Aes256Gcm, CipherSuite, HkdfSha256 } from '@hpke/core';
import { DhkemX25519HkdfSha256 } from '@hpke/dhkem-x25519';

import vectors from './__fixtures__/p2_core_crypto.json';
import {
  BLIND_BOX_SCHEME,
  base64ToBytes,
  buildWrapMeta,
  bytesToBase64,
  bytesFromHex,
  bytesToHexString,
  derivePasskeyKek,
  newVmk,
  passkeyPrfSalts,
  releaseProtectedDek,
  sealBlindBox,
  sealProtected,
  signDigest,
  SIGN_SCHEME_ECDSA_SECP256K1_DER,
  SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE,
  SIGN_SCHEME_SCHNORR_SECP256K1_BIP340,
  unwrapProtectedDek,
  unwrapVmk,
  webAuthnPrfExtensionInput,
  type SignatureScheme,
} from './vaultCrypto';

type P2Vectors = typeof vectors;

const p2 = vectors as P2Vectors;
const encoder = new TextEncoder();

function arrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const out = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(out).set(bytes);
  return out;
}

async function hkdfSha256(ikm: Uint8Array, salt: string, info: string, length: number): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey('raw', arrayBuffer(ikm), 'HKDF', false, ['deriveBits']);
  return new Uint8Array(
    await crypto.subtle.deriveBits(
      {
        name: 'HKDF',
        hash: 'SHA-256',
        salt: encoder.encode(salt),
        info: encoder.encode(info),
      },
      key,
      length * 8,
    ),
  );
}

async function avaultVectorReceiverKey(): Promise<CryptoKeyPair> {
  const suite = new CipherSuite({
    kem: new DhkemX25519HkdfSha256(),
    kdf: new HkdfSha256(),
    aead: new Aes256Gcm(),
  });
  const ikm = await hkdfSha256(
    bytesFromHex(p2.blind_box.master_key_hex),
    'avault:blind-box:receiver-salt:v1',
    'avault:blind-box:receiver-x25519:v1',
    32,
  );
  return suite.kem.deriveKeyPair(ikm);
}

async function openBlindBox(box: { enc: string; ct: string }): Promise<Uint8Array> {
  const suite = new CipherSuite({
    kem: new DhkemX25519HkdfSha256(),
    kdf: new HkdfSha256(),
    aead: new Aes256Gcm(),
  });
  return new Uint8Array(
    await suite.open(
      {
        recipientKey: (await avaultVectorReceiverKey()).privateKey,
        enc: base64ToBytes(box.enc),
        info: encoder.encode(p2.blind_box.hpke_info_utf8),
      },
      base64ToBytes(box.ct),
      encoder.encode(p2.blind_box.aad_utf8),
    ),
  );
}

describe('vaultCrypto signing vectors', () => {
  it('matches avault secp256k1 signatures byte-for-byte', () => {
    for (const vector of p2.signing.schemes) {
      const scheme = vector.scheme as SignatureScheme;
      const result = signDigest(p2.signing.private_key_hex, p2.signing.digest_hex, scheme, {
        schnorrAuxRand:
          scheme === SIGN_SCHEME_SCHNORR_SECP256K1_BIP340 ? p2.signing.schnorr_aux_rand_hex : undefined,
      });

      expect(result.signature).toBe(vector.signature_hex);
      expect(result.recovery_id).toBe(vector.recovery_id);
    }
  });

  it('keeps the ECDSA recoverable signature as r||s plus recovery id', () => {
    const result = signDigest(
      p2.signing.private_key_hex,
      p2.signing.digest_hex,
      SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE,
    );

    expect(result.signature).toHaveLength(128);
    expect(result.recovery_id).toBe(0);
  });

  it('returns DER and BIP340 encodings for their wire schemes', () => {
    const der = signDigest(p2.signing.private_key_hex, p2.signing.digest_hex, SIGN_SCHEME_ECDSA_SECP256K1_DER);
    const schnorr = signDigest(
      p2.signing.private_key_hex,
      p2.signing.digest_hex,
      SIGN_SCHEME_SCHNORR_SECP256K1_BIP340,
      { schnorrAuxRand: p2.signing.schnorr_aux_rand_hex },
    );

    expect(der.signature.startsWith('30')).toBe(true);
    expect(schnorr.signature).toHaveLength(128);
    expect(der.recovery_id).toBeNull();
    expect(schnorr.recovery_id).toBeNull();
  });
});

describe('vaultCrypto blind boxes', () => {
  it('opens the shared avault blind-box vector with the Phase A receiver key', async () => {
    await expect(openBlindBox(p2.blind_box.box)).resolves.toEqual(bytesFromHex(p2.blind_box.plaintext_hex));
  });

  it('seals to the avault public key with the Phase A JSON shape', async () => {
    const plaintext = encoder.encode('blind secret');
    const box = await sealBlindBox(base64ToBytes(bytesToBase64(plaintext)), {
      public_key: p2.blind_box.public_key,
      fingerprint: p2.blind_box.fingerprint,
    });

    expect(box.scheme).toBe(BLIND_BOX_SCHEME);
    expect(base64ToBytes(box.enc)).toHaveLength(32);
    expect(base64ToBytes(box.ct).length).toBeGreaterThan(16);
    expect(Object.keys(box).sort()).toEqual(['ct', 'enc', 'scheme']);
    await expect(openBlindBox(box)).resolves.toEqual(plaintext);
  });

  it('rejects a substituted avault public key when a fingerprint is pinned', async () => {
    await expect(
      sealBlindBox('x', {
        public_key: p2.blind_box.public_key,
        fingerprint: '00'.repeat(32),
      }),
    ).rejects.toThrow(/fingerprint/);
  });
});

describe('vaultCrypto protected hierarchy', () => {
  it('unwraps the VMK with either passkey PRF or fallback password', async () => {
    const vmk = newVmk();
    const prfSalt = new Uint8Array(32).fill(0x11);
    const prfOutput = new Uint8Array(32).fill(0x22);
    const wrapMeta = await buildWrapMeta(vmk, [
      { kind: 'passkey', prfOutput, prfSalt, credentialId: 'cred-1' },
      { kind: 'password', password: 'less-secure-fallback', argon2id: { memorySize: 512, iterations: 2 } },
    ]);

    await expect(unwrapVmk(wrapMeta, { kind: 'passkey', prfOutput })).resolves.toEqual(vmk);
    await expect(unwrapVmk(wrapMeta, 'less-secure-fallback')).resolves.toEqual(vmk);
    expect(passkeyPrfSalts(wrapMeta)).toEqual([prfSalt]);
    expect(webAuthnPrfExtensionInput(prfSalt).prf.eval.first.byteLength).toBe(32);
  });

  it('wraps a protected value with a per-record DEK and releases only that DEK', async () => {
    const vmk = newVmk();
    const sealed = await sealProtected(new TextEncoder().encode('protected value'), vmk);
    const dek = await unwrapProtectedDek(sealed, vmk);
    const released = await releaseProtectedDek(sealed, vmk, p2.blind_box.public_key);

    expect(dek).toHaveLength(32);
    expect(released.scheme).toBe(BLIND_BOX_SCHEME);
    expect(base64ToBytes(released.enc)).toHaveLength(32);
    expect(base64ToBytes(released.ct).length).toBe(32 + 16);
  });

  it('derives a stable passkey KEK from WebAuthn PRF output and salt', async () => {
    const prfOutput = new Uint8Array(32).fill(7);
    const prfSalt = new Uint8Array(32).fill(9);

    expect(bytesToHexString(await derivePasskeyKek(prfOutput, prfSalt))).toBe(
      bytesToHexString(await derivePasskeyKek(prfOutput, prfSalt)),
    );
  });
});
