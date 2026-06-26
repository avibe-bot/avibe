import { describe, expect, it } from 'vitest';
import { Aes256Gcm, CipherSuite, HkdfSha256 } from '@hpke/core';
import { DhkemX25519HkdfSha256 } from '@hpke/dhkem-x25519';

import vectors from './__fixtures__/p2_core_crypto.json';
import {
  BLIND_BOX_AAD_DOMAIN,
  BLIND_BOX_SCHEME,
  base64ToBytes,
  blindBoxAad,
  blindBoxAadHex,
  blindBoxAgentDeliverOperationHash,
  blindBoxAgentSignOperationHash,
  blindBoxOperationHash,
  blindBoxSignOperationHash,
  buildWrapMeta,
  bytesToBase64,
  bytesFromHex,
  bytesToHexString,
  derivePasskeyKek,
  newVmk,
  passkeyPrfSalts,
  protectedDekReleaseBlindBoxContext,
  protectedRecordAadHex,
  releaseProtectedDek,
  sealBlindBox,
  sealProtected,
  signDigest,
  SIGN_SCHEME_ECDSA_SECP256K1_DER,
  SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE,
  SIGN_SCHEME_SCHNORR_SECP256K1_BIP340,
  standardCreateBlindBoxContext,
  unwrapProtectedDek,
  unwrapVmk,
  WRAP_META_VERSION,
  WRAP_SCHEME,
  webAuthnPrfExtensionInput,
  type SignatureScheme,
  type BlindBoxContext,
} from './vaultCrypto';

type P2Vectors = typeof vectors;

const p2 = vectors as P2Vectors;
const encoder = new TextEncoder();
const vectorBlindBoxName = p2.blind_box.context.name;

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

async function openBlindBox(box: { enc: string; ct: string }, aad: Uint8Array): Promise<Uint8Array> {
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
      aad,
    ),
  );
}

function approvalFromVector(vector: (typeof p2.blind_box_aad_examples.cases)[number]) {
  return {
    nonce: bytesFromHex(vector.approval_nonce_hex),
    expiresAtUnix: vector.approval_expires_at_unix ?? 0,
  };
}

function blindBoxContextFromVector(vector: (typeof p2.blind_box_aad_examples.cases)[number]): BlindBoxContext {
  switch (vector.purpose) {
    case 'seal':
      return standardCreateBlindBoxContext(vector.name);
    case 'deliver':
      return protectedDekReleaseBlindBoxContext(vector.name, {
        kind: 'deliver',
        approval: approvalFromVector(vector),
        operationHash: vector.operation_hash_hex,
      });
    case 'sign':
      return protectedDekReleaseBlindBoxContext(vector.name, {
        kind: 'sign',
        signatureScheme: vector.sign_scheme as SignatureScheme,
        digest: vector.digest_hex,
        approval: approvalFromVector(vector),
        operationHash: vector.operation_hash_hex,
      });
    case 'agent-deliver':
      return protectedDekReleaseBlindBoxContext(vector.name, {
        kind: 'agent-deliver',
        scopeType: vector.scope_type,
        scopeRef: vector.scope_ref,
        approval: approvalFromVector(vector),
        operationHash: vector.operation_hash_hex,
      });
    case 'agent-sign':
      return protectedDekReleaseBlindBoxContext(vector.name, {
        kind: 'agent-sign',
        scopeType: vector.scope_type,
        scopeRef: vector.scope_ref,
        signatureScheme: vector.sign_scheme as SignatureScheme,
        digest: vector.digest_hex,
        approval: approvalFromVector(vector),
        operationHash: vector.operation_hash_hex,
      });
    default:
      throw new Error(`unexpected blind-box AAD vector purpose: ${vector.purpose}`);
  }
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
  it('matches all shared avault blind-box AAD examples byte-for-byte', () => {
    expect(p2.blind_box.aad_domain_utf8).toBe(BLIND_BOX_AAD_DOMAIN);
    expect(p2.blind_box_aad_examples.wrap_scheme).toBe(WRAP_SCHEME);
    expect(p2.blind_box_aad_examples.version).toBe(WRAP_META_VERSION);
    for (const vector of p2.blind_box_aad_examples.cases) {
      expect(blindBoxAadHex(blindBoxContextFromVector(vector))).toBe(vector.aad_hex);
    }
  });

  it('derives shared operation hash examples from length-prefixed fields', async () => {
    const sign = p2.blind_box_aad_examples.cases.find((vector) => vector.purpose === 'sign');
    const agentDeliver = p2.blind_box_aad_examples.cases.find((vector) => vector.purpose === 'agent-deliver');
    const agentSign = p2.blind_box_aad_examples.cases.find((vector) => vector.purpose === 'agent-sign');
    if (!sign || !agentDeliver || !agentSign) {
      throw new Error('missing shared operation-hash examples');
    }

    await expect(
      blindBoxOperationHash(['sign', sign.sign_scheme, bytesFromHex(sign.digest_hex)]).then(bytesToHexString),
    ).resolves.toBe(sign.operation_hash_hex);
    await expect(
      blindBoxSignOperationHash(sign.sign_scheme as SignatureScheme, sign.digest_hex).then(bytesToHexString),
    ).resolves.toBe(sign.operation_hash_hex);
    await expect(blindBoxOperationHash(['agent-deliver', agentDeliver.name]).then(bytesToHexString)).resolves.toBe(
      agentDeliver.operation_hash_hex,
    );
    await expect(blindBoxAgentDeliverOperationHash(agentDeliver.name).then(bytesToHexString)).resolves.toBe(
      agentDeliver.operation_hash_hex,
    );
    await expect(
      blindBoxOperationHash(['agent-sign', agentSign.sign_scheme, bytesFromHex(agentSign.digest_hex)]).then(
        bytesToHexString,
      ),
    ).resolves.toBe(agentSign.operation_hash_hex);
    await expect(
      blindBoxAgentSignOperationHash(agentSign.sign_scheme as SignatureScheme, agentSign.digest_hex).then(
        bytesToHexString,
      ),
    ).resolves.toBe(agentSign.operation_hash_hex);
  });

  it('opens the shared avault blind-box vector with the recorded operation-context AAD', async () => {
    const context = standardCreateBlindBoxContext(p2.blind_box.context.name);
    expect(blindBoxAadHex(context)).toBe(p2.blind_box.aad_hex);
    await expect(openBlindBox(p2.blind_box.box, blindBoxAad(context))).resolves.toEqual(
      bytesFromHex(p2.blind_box.plaintext_hex),
    );
  });

  it('seals to the avault public key with the operation-context AAD', async () => {
    const plaintext = encoder.encode('blind secret');
    const context = standardCreateBlindBoxContext(vectorBlindBoxName);
    const box = await sealBlindBox(base64ToBytes(bytesToBase64(plaintext)), {
      public_key: p2.blind_box.public_key,
      fingerprint: p2.blind_box.fingerprint,
    }, context);

    expect(box.scheme).toBe(BLIND_BOX_SCHEME);
    expect(base64ToBytes(box.enc)).toHaveLength(32);
    expect(base64ToBytes(box.ct).length).toBeGreaterThan(16);
    expect(Object.keys(box).sort()).toEqual(['ct', 'enc', 'scheme']);
    await expect(openBlindBox(box, blindBoxAad(context))).resolves.toEqual(plaintext);
    await expect(openBlindBox(box, blindBoxAad(standardCreateBlindBoxContext('OTHER_SECRET')))).rejects.toThrow();
  });

  it('rejects a substituted avault public key when a fingerprint is pinned', async () => {
    await expect(
      sealBlindBox('x', {
        public_key: p2.blind_box.public_key,
        fingerprint: '00'.repeat(32),
      }, standardCreateBlindBoxContext(vectorBlindBoxName)),
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
    const recordContext = { name: 'ETH_SIGNING_KEY' };
    const sealed = await sealProtected(new TextEncoder().encode('protected value'), vmk, recordContext);
    const dek = await unwrapProtectedDek(sealed, vmk, recordContext);
    const sign = p2.blind_box_aad_examples.cases.find((vector) => vector.purpose === 'sign');
    if (!sign) {
      throw new Error('missing shared sign AAD example');
    }
    const context = protectedDekReleaseBlindBoxContext('ETH_SIGNING_KEY', {
      kind: 'sign',
      digest: sign.digest_hex,
      signatureScheme: sign.sign_scheme as SignatureScheme,
      approval: approvalFromVector(sign),
      operationHash: sign.operation_hash_hex,
    });
    const avaultPublicKey = {
      public_key: p2.blind_box.public_key,
      fingerprint: p2.blind_box.fingerprint,
    };
    const released = await releaseProtectedDek(sealed, vmk, avaultPublicKey, recordContext, context);

    expect(dek).toHaveLength(32);
    expect(protectedRecordAadHex(recordContext)).not.toBe(protectedRecordAadHex({ name: 'OTHER_SIGNING_KEY' }));
    expect(released.scheme).toBe(BLIND_BOX_SCHEME);
    expect(base64ToBytes(released.enc)).toHaveLength(32);
    expect(base64ToBytes(released.ct).length).toBe(32 + 16);
    await expect(unwrapProtectedDek(sealed, vmk, { name: 'OTHER_SIGNING_KEY' })).rejects.toThrow();
    await expect(releaseProtectedDek(sealed, vmk, { public_key: p2.blind_box.public_key }, recordContext, context)).rejects.toThrow(
      /fingerprint/,
    );
    await expect(openBlindBox(released, blindBoxAad(context))).resolves.toEqual(dek);
    await expect(
      openBlindBox(
        released,
        blindBoxAad(
          protectedDekReleaseBlindBoxContext('ETH_SIGNING_KEY', {
            kind: 'sign',
            digest: 'ff'.repeat(32),
            signatureScheme: SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE,
            approval: approvalFromVector(sign),
            operationHash: sign.operation_hash_hex,
          }),
        ),
      ),
    ).rejects.toThrow();
  });

  it('derives a stable passkey KEK from WebAuthn PRF output and salt', async () => {
    const prfOutput = new Uint8Array(32).fill(7);
    const prfSalt = new Uint8Array(32).fill(9);

    expect(bytesToHexString(await derivePasskeyKek(prfOutput, prfSalt))).toBe(
      bytesToHexString(await derivePasskeyKek(prfOutput, prfSalt)),
    );
  });
});
