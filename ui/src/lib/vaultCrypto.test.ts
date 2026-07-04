import { describe, expect, it } from 'vitest';
import { Aes256Gcm, CipherSuite, HkdfSha256 } from '@hpke/core';
import { DhkemX25519HkdfSha256 } from '@hpke/dhkem-x25519';
import { scrypt } from 'hash-wasm';

import vectors from './__fixtures__/p2_core_crypto.json';
import {
  BLIND_BOX_AAD_DOMAIN,
  BLIND_BOX_SCHEME,
  addPasswordCopy,
  avaultPublicKeyFingerprint,
  base64ToBytes,
  blindBoxAad,
  blindBoxAadHex,
  blindBoxAgentDeliverOperationHash,
  blindBoxAgentSignOperationHash,
  blindBoxOperationHash,
  buildWrapMeta,
  bytesToBase64,
  bytesFromHex,
  bytesToHexString,
  derivePasskeyKek,
  generateSigningKey,
  importSigningKey,
  newVmk,
  passkeyPrfSaltEntries,
  passkeyPrfSalts,
  protectedDekReleaseBlindBoxContext,
  protectedRecordAadHex,
  releaseProtectedDek,
  sealBlindBox,
  sealProtected,
  openProtected,
  packProtectedRecord,
  unpackProtectedRecord,
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

async function aesgcmEncryptForTest(key: Uint8Array, nonce: Uint8Array, plaintext: Uint8Array): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey('raw', arrayBuffer(key), 'AES-GCM', false, ['encrypt']);
  return new Uint8Array(
    await crypto.subtle.encrypt(
      {
        name: 'AES-GCM',
        iv: arrayBuffer(nonce),
      },
      cryptoKey,
      arrayBuffer(plaintext),
    ),
  );
}

async function malformedScryptVmkCopy(password: string, plaintext: Uint8Array) {
  const salt = new Uint8Array(16).fill(0xa5);
  const nonce = new Uint8Array(12).fill(0x5a);
  const kek = (await scrypt({
    password: encoder.encode(password),
    salt,
    costFactor: 2 ** 15,
    blockSize: 8,
    parallelism: 1,
    hashLength: 32,
    outputType: 'binary',
  })) as Uint8Array;
  const wrapped = await aesgcmEncryptForTest(kek, nonce, plaintext);
  return {
    kind: 'password',
    kdf: 'scrypt',
    n: 2 ** 15,
    r: 8,
    p: 1,
    salt: bytesToBase64(salt),
    nonce: bytesToBase64(nonce),
    wrapped: bytesToBase64(wrapped),
  };
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

async function blindBoxContextFromVector(vector: (typeof p2.blind_box_aad_examples.cases)[number]): Promise<BlindBoxContext> {
  switch (vector.purpose) {
    case 'seal':
      return standardCreateBlindBoxContext(vector.name);
    case 'agent-deliver':
      return await protectedDekReleaseBlindBoxContext(vector.name, {
        kind: 'agent-deliver',
        grantId: vector.grant_id,
        ttlSecs: vector.ttl_secs,
        approval: approvalFromVector(vector),
        operationHash: vector.operation_hash_hex,
      });
    case 'agent-sign':
      return await protectedDekReleaseBlindBoxContext(vector.name, {
        kind: 'agent-sign',
        grantId: vector.grant_id,
        signatureScheme: vector.sign_scheme as SignatureScheme,
        digest: vector.digest_hex,
        ttlSecs: vector.ttl_secs,
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
  it('matches all shared avault blind-box AAD examples byte-for-byte', async () => {
    expect(p2.blind_box.aad_domain_utf8).toBe(BLIND_BOX_AAD_DOMAIN);
    expect(p2.blind_box_aad_examples.wrap_scheme).toBe(WRAP_SCHEME);
    expect(p2.blind_box_aad_examples.version).toBe(WRAP_META_VERSION);
    for (const vector of p2.blind_box_aad_examples.cases) {
      expect(blindBoxAadHex(await blindBoxContextFromVector(vector))).toBe(vector.aad_hex);
    }
  });

  it('derives shared operation hash examples from length-prefixed fields', async () => {
    const agentDeliver = p2.blind_box_aad_examples.cases.find((vector) => vector.purpose === 'agent-deliver');
    const agentSign = p2.blind_box_aad_examples.cases.find((vector) => vector.purpose === 'agent-sign');
    if (!agentDeliver || !agentSign) {
      throw new Error('missing shared operation-hash examples');
    }

    for (const vector of p2.blind_box_aad_examples.cases) {
      if (!vector.operation_hash_hex || !vector.operation_hash_fields_hex) {
        continue;
      }
      await expect(
        blindBoxOperationHash(vector.operation_hash_fields_hex.map(bytesFromHex)).then(bytesToHexString),
      ).resolves.toBe(vector.operation_hash_hex);
    }
    await expect(
      blindBoxAgentDeliverOperationHash(agentDeliver.name, agentDeliver.ttl_secs).then(bytesToHexString),
    ).resolves.toBe(agentDeliver.operation_hash_hex);
    await expect(
      blindBoxAgentSignOperationHash(
        agentSign.sign_scheme as SignatureScheme,
        agentSign.digest_hex,
        agentSign.ttl_secs,
      ).then(bytesToHexString),
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

  it('enforces the daemon vault secret-name syntax before producing AAD', async () => {
    expect(() => standardCreateBlindBoxContext('OPENAI_API_KEY')).not.toThrow();
    expect(() => standardCreateBlindBoxContext('openAi_key')).not.toThrow();
    expect(() => standardCreateBlindBoxContext(' OPENAI_API_KEY ')).toThrow(/secret name/);
    expect(() => standardCreateBlindBoxContext('1OPENAI_KEY')).toThrow(/secret name/);
    await expect(
      protectedDekReleaseBlindBoxContext('1OPENAI_KEY', {
        kind: 'agent-deliver',
        grantId: 'gr_123',
        ttlSecs: 60,
        approval: { nonce: new Uint8Array(16).fill(1), expiresAtUnix: 1 },
        operationHash: '00'.repeat(32),
      }),
    ).rejects.toThrow(/secret name/);
  });
});

describe('vaultCrypto protected hierarchy', () => {
  it('unwraps the VMK with either passkey PRF or fallback password', async () => {
    const vmk = newVmk();
    const prfSalt = new Uint8Array(32).fill(0x11);
    const prfOutput = new Uint8Array(32).fill(0x22);
    const wrapMeta = await buildWrapMeta(vmk, [
      { kind: 'passkey', prfOutput, prfSalt, credentialId: 'cred-1' },
      { kind: 'password', password: 'less-secure-fallback' },
    ]);

    await expect(unwrapVmk(wrapMeta, { kind: 'passkey', prfOutput })).resolves.toEqual(vmk);
    await expect(unwrapVmk(wrapMeta, 'less-secure-fallback')).resolves.toEqual(vmk);
    expect(JSON.parse(wrapMeta).copies.find((copy: { kind: string }) => copy.kind === 'password')?.kdf).toBe('scrypt');
    expect(passkeyPrfSalts(wrapMeta)).toEqual([prfSalt]);
    expect(passkeyPrfSaltEntries(wrapMeta)).toEqual([{ prfSalt, credentialId: 'cred-1' }]);
    expect(webAuthnPrfExtensionInput(prfSalt).prf.eval.first.byteLength).toBe(32);

    const withArgon2id = await addPasswordCopy(wrapMeta, vmk, 'argon2id-fallback', {
      memorySize: 512,
      iterations: 2,
    });
    const copies = JSON.parse(withArgon2id).copies as Array<{ kind: string; kdf?: string }>;
    expect(copies.at(-1)?.kdf).toBe('argon2id');
  });

  it('keeps setup passkey PRF bytes and credential id intact through wrap_meta', async () => {
    const vmk = newVmk();
    const prfSalt = new Uint8Array(32).fill(0x11);
    const prfOutput = new Uint8Array(32).fill(0x22);
    const credentialId = '/Wz4YbtaoR9NBzQH1amGZagS0BmEdIh2dS8OIbpdN2WftQMJlny4PP4I';

    const wrapMeta = await buildWrapMeta(vmk, [{ kind: 'passkey', prfOutput, prfSalt, credentialId }]);

    await expect(unwrapVmk(wrapMeta, { kind: 'passkey', prfOutput })).resolves.toEqual(vmk);
    expect(passkeyPrfSaltEntries(wrapMeta)).toEqual([{ prfSalt, credentialId }]);
    expect(prfOutput).toEqual(new Uint8Array(32).fill(0x22));
  });

  it('skips malformed authenticated VMK copies and tries the next valid copy', async () => {
    const vmk = newVmk();
    const password = 'less-secure-fallback';
    const malformed = await malformedScryptVmkCopy(password, new Uint8Array(31).fill(0x44));
    const valid = await buildWrapMeta(vmk, [password]);
    const combined = JSON.stringify({
      v: 1,
      copies: [malformed, ...JSON.parse(valid).copies],
    });

    await expect(unwrapVmk(combined, password)).resolves.toEqual(vmk);
  });

  it('rejects blank password wrap factors before deriving VMK copies', async () => {
    const vmk = newVmk();

    await expect(buildWrapMeta(vmk, [''])).rejects.toThrow(/password/);
    await expect(buildWrapMeta(vmk, [{ kind: 'password', password: '   ' }])).rejects.toThrow(/password/);
    await expect(addPasswordCopy(JSON.stringify({ v: 1, copies: [] }), vmk, '')).rejects.toThrow(/password/);
  });

  it('wraps a protected value with a per-record DEK and releases only that DEK', async () => {
    const vmk = newVmk();
    const recordContext = { name: 'OPENAI_API_KEY' };
    const sealed = await sealProtected(new TextEncoder().encode('protected value'), vmk, recordContext);
    const dek = await unwrapProtectedDek(sealed, vmk, recordContext);
    const agentDeliver = p2.blind_box_aad_examples.cases.find((vector) => vector.purpose === 'agent-deliver');
    const agentSign = p2.blind_box_aad_examples.cases.find((vector) => vector.purpose === 'agent-sign');
    if (!agentDeliver || !agentSign) {
      throw new Error('missing shared agent AAD examples');
    }
    const context = await protectedDekReleaseBlindBoxContext('OPENAI_API_KEY', {
      kind: 'agent-deliver',
      grantId: agentDeliver.grant_id,
      ttlSecs: agentDeliver.ttl_secs,
      approval: approvalFromVector(agentDeliver),
      operationHash: agentDeliver.operation_hash_hex,
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
    await expect(
      releaseProtectedDek(
        { ...sealed, wrapped_dek: bytesToBase64(new Uint8Array(48).fill(0x7f)) },
        vmk,
        { public_key: p2.blind_box.public_key, fingerprint: '00'.repeat(32) },
        recordContext,
        context,
      ),
    ).rejects.toThrow(/fingerprint/);
    const badPublicKey = {
      public_key: bytesToBase64(new Uint8Array(31).fill(0x5a)),
    };
    await expect(
      releaseProtectedDek(
        { ...sealed, wrapped_dek: bytesToBase64(new Uint8Array(48).fill(0x7f)) },
        vmk,
        { ...badPublicKey, fingerprint: await avaultPublicKeyFingerprint(badPublicKey) },
        recordContext,
        context,
      ),
    ).rejects.toThrow(/public key/);
    await expect(releaseProtectedDek(sealed, vmk, avaultPublicKey, { name: 'OTHER_SIGNING_KEY' }, context)).rejects.toThrow(
      /record name/,
    );
    await expect(openBlindBox(released, blindBoxAad(context))).resolves.toEqual(dek);
    await expect(
      protectedDekReleaseBlindBoxContext('OPENAI_API_KEY', {
        kind: 'agent-deliver',
        grantId: agentDeliver.grant_id,
        ttlSecs: 999,
        approval: approvalFromVector(agentDeliver),
        operationHash: agentDeliver.operation_hash_hex,
      }),
    ).rejects.toThrow(/operation hash/);
    await expect(
      releaseProtectedDek(
        { ...sealed, wrapped_dek: bytesToBase64(new Uint8Array(48).fill(0x7f)) },
        vmk,
        avaultPublicKey,
        recordContext,
        { ...context, operationHash: new Uint8Array(32).fill(0x42) },
      ),
    ).rejects.toThrow(/operation hash/);
    const signContext = await protectedDekReleaseBlindBoxContext('OPENAI_API_KEY', {
      kind: 'agent-sign',
      grantId: agentSign.grant_id,
      digest: agentSign.digest_hex,
      ttlSecs: agentSign.ttl_secs,
      signatureScheme: agentSign.sign_scheme as SignatureScheme,
      approval: approvalFromVector(agentSign),
      operationHash: agentSign.operation_hash_hex,
    });
    await expect(releaseProtectedDek(sealed, vmk, avaultPublicKey, recordContext, signContext)).rejects.toThrow(
      /signed locally/,
    );
    await expect(
      openBlindBox(
        released,
        blindBoxAad(
          await protectedDekReleaseBlindBoxContext('OPENAI_API_KEY', {
            kind: 'agent-deliver',
            grantId: 'other-grant',
            ttlSecs: agentDeliver.ttl_secs,
            approval: approvalFromVector(agentDeliver),
            operationHash: agentDeliver.operation_hash_hex,
          }),
        ),
      ),
    ).rejects.toThrow();
  });

  it('copies ArrayBuffer inputs before zeroizing local buffers', () => {
    const key = bytesFromHex(p2.signing.private_key_hex);
    const keyBuffer = key.buffer.slice(key.byteOffset, key.byteOffset + key.byteLength);
    signDigest(keyBuffer, p2.signing.digest_hex, SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE);
    expect(bytesToHexString(new Uint8Array(keyBuffer))).toBe(p2.signing.private_key_hex);
  });

  it('does not wipe caller-owned VMK ArrayBuffers when unwrapping protected DEKs', async () => {
    const vmk = newVmk();
    const vmkBuffer = vmk.buffer.slice(vmk.byteOffset, vmk.byteOffset + vmk.byteLength);
    const sealed = await sealProtected(new TextEncoder().encode('protected value'), vmkBuffer, { name: 'OPENAI_API_KEY' });
    await expect(unwrapProtectedDek(sealed, vmkBuffer, { name: 'OPENAI_API_KEY' })).resolves.toHaveLength(32);
    expect(bytesToHexString(new Uint8Array(vmkBuffer))).toBe(bytesToHexString(vmk));
  });

  it('does not wipe caller-owned protected plaintext ArrayBuffers after sealing', async () => {
    const vmk = newVmk();
    const plaintext = new TextEncoder().encode('protected value');
    const plaintextBuffer = plaintext.buffer.slice(plaintext.byteOffset, plaintext.byteOffset + plaintext.byteLength);
    await sealProtected(plaintextBuffer, vmk, { name: 'OPENAI_API_KEY' });

    expect(new TextDecoder().decode(plaintextBuffer)).toBe('protected value');
  });

  it('derives a stable passkey KEK from WebAuthn PRF output and salt', async () => {
    const prfOutput = new Uint8Array(32).fill(7);
    const prfSalt = new Uint8Array(32).fill(9);

    expect(bytesToHexString(await derivePasskeyKek(prfOutput, prfSalt))).toBe(
      bytesToHexString(await derivePasskeyKek(prfOutput, prfSalt)),
    );
  });
});

describe('protected record storage composition', () => {
  it('packs a sealed record and unpacks it for a clean open round-trip', async () => {
    const vmk = newVmk();
    const wrapMeta = await buildWrapMeta(vmk, ['vault-password']);
    const context = { name: 'OPENAI_API_KEY' };
    const sealed = await sealProtected(new TextEncoder().encode('sk-secret-value'), vmk, context);

    const envelope = packProtectedRecord(sealed, wrapMeta);
    const restored = unpackProtectedRecord(envelope);
    const vmkAgain = await unwrapVmk(restored.vmkWrapMeta, 'vault-password');
    const opened = await openProtected(restored.sealed, vmkAgain, context);

    expect(new TextDecoder().decode(opened)).toBe('sk-secret-value');
  });

  it('refuses to fold a DEK into a wrap_meta that already carries one', async () => {
    const vmk = newVmk();
    const wrapMeta = await buildWrapMeta(vmk, ['pw']);
    const sealed = await sealProtected(new TextEncoder().encode('v'), vmk, { name: 'NAME' });
    const once = packProtectedRecord(sealed, wrapMeta);
    expect(() => packProtectedRecord(sealed, once.wrap_meta)).toThrow();
  });
});

describe('vaultCrypto signing key generation', () => {
  it('generates a valid, chain-agnostic secp256k1 signing key', () => {
    const key = generateSigningKey();
    expect(key.privateKey).toBeInstanceOf(Uint8Array);
    expect(key.privateKey.length).toBe(32);
    // Compressed secp256k1 public key: 33 bytes (66 hex), 0x02/0x03 prefix.
    expect(key.publicKey).toMatch(/^0[23][0-9a-f]{64}$/);
    // The key works with the supported signing schemes (signDigest copies its
    // input, so the private key stays usable afterwards).
    expect(() =>
      signDigest(key.privateKey, p2.signing.digest_hex, SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE),
    ).not.toThrow();
    expect(key.privateKey.some((b) => b !== 0)).toBe(true);
  });

  it('derives the same public key on import as on generation', () => {
    const key = generateSigningKey();
    expect(importSigningKey(key.privateKey).publicKey).toBe(key.publicKey);
  });

  it('imports a known private key deterministically (hex)', () => {
    const a = importSigningKey(p2.signing.private_key_hex);
    const b = importSigningKey(p2.signing.private_key_hex);
    expect(a.publicKey).toBe(b.publicKey);
    expect(a.publicKey).toMatch(/^0[23][0-9a-f]{64}$/);
  });

  it('rejects an invalid private key', () => {
    expect(() => importSigningKey('00'.repeat(32))).toThrow(); // zero scalar
    expect(() => importSigningKey('zz')).toThrow(); // not hex / wrong length
  });
});
