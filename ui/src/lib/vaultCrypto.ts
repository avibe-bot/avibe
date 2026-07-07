import { Aes256Gcm, CipherSuite, HkdfSha256 } from '@hpke/core';
import { DhkemX25519HkdfSha256 } from '@hpke/dhkem-x25519';
import { secp256k1, schnorr } from '@noble/curves/secp256k1.js';
import { bytesToHex, hexToBytes } from '@noble/curves/utils.js';

export const BLIND_BOX_SCHEME = 'hpke-x25519-hkdfsha256-aes256gcm-v1';
export const BLIND_BOX_HPKE_INFO = 'avault:blind-box:v1';
export const BLIND_BOX_AAD_DOMAIN = 'avault:blind-box:aad:v1';
export const WRAP_SCHEME = 'machine-aesgcm-v1';
export const WRAP_META_VERSION = 1;
export const SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE = 'ecdsa-secp256k1-recoverable';
export const SIGN_SCHEME_ECDSA_SECP256K1_DER = 'ecdsa-secp256k1-der';
export const SIGN_SCHEME_SCHNORR_SECP256K1_BIP340 = 'schnorr-secp256k1-bip340';

const KEY_BYTES = 32;
const NONCE_BYTES = 12;
const PASSKEY_PRF_SALT_BYTES = 32;
const PASSKEY_HKDF_INFO = 'avault:protected-vmk:kek-passkey:v1';
const SECRET_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

const textEncoder = new TextEncoder();

type BytesLike = Uint8Array | ArrayBuffer | ArrayBufferView;
type HexOrBytes = BytesLike | string;

export type BlindBox = {
  scheme: typeof BLIND_BOX_SCHEME;
  enc: string;
  ct: string;
};

export type AvaultPublicKey = {
  public_key: string;
  fingerprint?: string;
};

export type ProtectedSealed = {
  ciphertext: string;
  nonce: string;
  dek_nonce: string;
  wrapped_dek: string;
};

export type ProtectedRecordContext = {
  name: string;
  scheme?: typeof WRAP_SCHEME;
  version?: typeof WRAP_META_VERSION;
};

export type PasskeyWrapFactor = {
  kind: 'passkey';
  prfOutput: BytesLike;
  prfSalt: BytesLike;
  credentialId?: string;
};

export type PasskeyUnlockFactor = {
  kind: 'passkey';
  prfOutput: BytesLike;
  prfSalt?: BytesLike;
};

export type SignatureScheme =
  | typeof SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE
  | typeof SIGN_SCHEME_ECDSA_SECP256K1_DER
  | typeof SIGN_SCHEME_SCHNORR_SECP256K1_BIP340;

export type SignatureResult = {
  scheme: SignatureScheme;
  signature: string;
  recovery_id: number | null;
};

export type SignDigestOptions = {
  schnorrAuxRand?: HexOrBytes;
};

export type BlindBoxApproval = {
  nonce: BytesLike;
  expiresAtUnix: number | bigint;
};

export type BlindBoxOperationHashField = string | BytesLike;

export type StandardCreateBlindBoxContext = {
  purpose: 'seal';
  name: string;
};

export type AgentDeliverBlindBoxContext = {
  purpose: 'agent-deliver';
  name: string;
  grantId: string;
  ttlSecs: number | bigint;
  approvalNonce: BytesLike;
  approvalExpiresAtUnix: number | bigint;
  operationHash: HexOrBytes;
};

export type AgentSignBlindBoxContext = {
  purpose: 'agent-sign';
  name: string;
  grantId: string;
  signScheme: SignatureScheme;
  digest: HexOrBytes;
  ttlSecs: number | bigint;
  approvalNonce: BytesLike;
  approvalExpiresAtUnix: number | bigint;
  operationHash: HexOrBytes;
};

export type ProtectedDekReleaseBlindBoxContext = AgentDeliverBlindBoxContext | AgentSignBlindBoxContext;
export type ProtectedDekDeliveryBlindBoxContext = AgentDeliverBlindBoxContext;

export type BlindBoxContext = StandardCreateBlindBoxContext | ProtectedDekReleaseBlindBoxContext;

export type ProtectedDekReleaseOperation =
  | {
      kind: 'agent-deliver';
      grantId: string;
      ttlSecs: number | bigint;
      approval: BlindBoxApproval;
      operationHash: HexOrBytes;
    }
  | {
      kind: 'agent-sign';
      grantId: string;
      signatureScheme: SignatureScheme;
      digest: HexOrBytes;
      ttlSecs: number | bigint;
      approval: BlindBoxApproval;
      operationHash: HexOrBytes;
    };

type WrapMeta = {
  v: 1;
  copies: PasskeyPrfCopy[];
};

type PasskeyPrfCopy = {
  kind: 'passkey';
  kdf: 'webauthn-prf-hkdf-sha256';
  prf_salt: string;
  nonce: string;
  wrapped: string;
  credential_id?: string;
};

export type WebAuthnPrfExtensionInput = {
  prf: {
    eval: {
      first: ArrayBuffer;
    };
  };
};

export type PasskeyPrfSalt = {
  prfSalt: Uint8Array;
  credentialId?: string;
};

function toUint8Array(value: BytesLike, field = 'bytes'): Uint8Array {
  if (value instanceof Uint8Array) {
    return new Uint8Array(value);
  }
  if (value instanceof ArrayBuffer) {
    return new Uint8Array(value.slice(0));
  }
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength));
  }
  throw new TypeError(`${field} must be bytes`);
}

function toArrayBuffer(value: BytesLike): ArrayBuffer {
  const bytes = toUint8Array(value);
  const out = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(out).set(bytes);
  bytes.fill(0);
  return out;
}

function utf8(value: string): Uint8Array {
  return textEncoder.encode(value);
}

function randomBytes(length: number): Uint8Array {
  const out = new Uint8Array(length);
  crypto.getRandomValues(out);
  return out;
}

export function bytesToBase64(value: BytesLike): string {
  const bytes = toUint8Array(value);
  let binary = '';
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

export function base64ToBytes(value: string): Uint8Array {
  return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
}

export function bytesFromHex(value: string): Uint8Array {
  return hexToBytes(value);
}

export function bytesToHexString(value: BytesLike): string {
  return bytesToHex(toUint8Array(value));
}

function assertLength(bytes: Uint8Array, length: number, field: string): void {
  if (bytes.length !== length) {
    throw new Error(`${field} must be ${length} bytes`);
  }
}

function wipeArrayBuffer(buffer: ArrayBuffer | undefined): void {
  if (buffer) {
    new Uint8Array(buffer).fill(0);
  }
}

async function aesgcmEncrypt(
  key: BytesLike,
  nonce: BytesLike,
  data: BytesLike,
  additionalData?: BytesLike,
): Promise<Uint8Array> {
  let keyBuffer: ArrayBuffer | undefined;
  let nonceBuffer: ArrayBuffer | undefined;
  let dataBuffer: ArrayBuffer | undefined;
  let additionalDataBuffer: ArrayBuffer | undefined;
  try {
    keyBuffer = toArrayBuffer(key);
    nonceBuffer = toArrayBuffer(nonce);
    dataBuffer = toArrayBuffer(data);
    additionalDataBuffer = additionalData ? toArrayBuffer(additionalData) : undefined;
    const cryptoKey = await crypto.subtle.importKey('raw', keyBuffer, 'AES-GCM', false, ['encrypt']);
    const ct = await crypto.subtle.encrypt(
      {
        name: 'AES-GCM',
        iv: nonceBuffer,
        ...(additionalDataBuffer ? { additionalData: additionalDataBuffer } : {}),
      },
      cryptoKey,
      dataBuffer,
    );
    return new Uint8Array(ct);
  } finally {
    wipeArrayBuffer(keyBuffer);
    wipeArrayBuffer(nonceBuffer);
    wipeArrayBuffer(dataBuffer);
    wipeArrayBuffer(additionalDataBuffer);
  }
}

async function aesgcmDecrypt(
  key: BytesLike,
  nonce: BytesLike,
  data: BytesLike,
  additionalData?: BytesLike,
): Promise<Uint8Array> {
  let keyBuffer: ArrayBuffer | undefined;
  let nonceBuffer: ArrayBuffer | undefined;
  let dataBuffer: ArrayBuffer | undefined;
  let additionalDataBuffer: ArrayBuffer | undefined;
  try {
    keyBuffer = toArrayBuffer(key);
    nonceBuffer = toArrayBuffer(nonce);
    dataBuffer = toArrayBuffer(data);
    additionalDataBuffer = additionalData ? toArrayBuffer(additionalData) : undefined;
    const cryptoKey = await crypto.subtle.importKey('raw', keyBuffer, 'AES-GCM', false, ['decrypt']);
    const pt = await crypto.subtle.decrypt(
      {
        name: 'AES-GCM',
        iv: nonceBuffer,
        ...(additionalDataBuffer ? { additionalData: additionalDataBuffer } : {}),
      },
      cryptoKey,
      dataBuffer,
    );
    return new Uint8Array(pt);
  } finally {
    wipeArrayBuffer(keyBuffer);
    wipeArrayBuffer(nonceBuffer);
    wipeArrayBuffer(dataBuffer);
    wipeArrayBuffer(additionalDataBuffer);
  }
}

async function hkdfSha256(ikm: BytesLike, salt: BytesLike, info: BytesLike, length: number): Promise<Uint8Array> {
  let ikmBuffer: ArrayBuffer | undefined;
  let saltBuffer: ArrayBuffer | undefined;
  let infoBuffer: ArrayBuffer | undefined;
  try {
    ikmBuffer = toArrayBuffer(ikm);
    saltBuffer = toArrayBuffer(salt);
    infoBuffer = toArrayBuffer(info);
    const key = await crypto.subtle.importKey('raw', ikmBuffer, 'HKDF', false, ['deriveBits']);
    const bits = await crypto.subtle.deriveBits(
      {
        name: 'HKDF',
        hash: 'SHA-256',
        salt: saltBuffer,
        info: infoBuffer,
      },
      key,
      length * 8,
    );
    return new Uint8Array(bits);
  } finally {
    wipeArrayBuffer(ikmBuffer);
    wipeArrayBuffer(saltBuffer);
    wipeArrayBuffer(infoBuffer);
  }
}

export async function derivePasskeyKek(prfOutput: BytesLike, prfSalt: BytesLike): Promise<Uint8Array> {
  const output = toUint8Array(prfOutput, 'passkey PRF output');
  try {
    const salt = toUint8Array(prfSalt, 'passkey PRF salt');
    // The WebAuthn PRF output length varies by authenticator/provider (e.g. 1Password
    // does not return exactly 32 bytes). HKDF accepts variable-length IKM, so require it
    // only be present rather than asserting an exact length, then derive a 32-byte KEK.
    if (output.length === 0) {
      throw new Error('passkey PRF output is empty');
    }
    assertLength(salt, PASSKEY_PRF_SALT_BYTES, 'passkey PRF salt');
    return await hkdfSha256(output, salt, utf8(PASSKEY_HKDF_INFO), KEY_BYTES);
  } finally {
    output.fill(0);
  }
}

export function newVmk(): Uint8Array {
  return randomBytes(KEY_BYTES);
}

export function newPasskeyPrfSalt(): Uint8Array {
  return randomBytes(PASSKEY_PRF_SALT_BYTES);
}

export function webAuthnPrfExtensionInput(prfSalt: BytesLike): WebAuthnPrfExtensionInput {
  const salt = toUint8Array(prfSalt, 'passkey PRF salt');
  assertLength(salt, PASSKEY_PRF_SALT_BYTES, 'passkey PRF salt');
  return { prf: { eval: { first: toArrayBuffer(salt) } } };
}

function parseWrapMeta(wrapMeta: string | WrapMeta): WrapMeta {
  const parsed = typeof wrapMeta === 'string' ? JSON.parse(wrapMeta) : wrapMeta;
  if (parsed?.v !== 1 || !Array.isArray(parsed.copies)) {
    throw new Error('invalid protected wrap_meta');
  }
  return parsed as WrapMeta;
}

async function passkeyCopy(vmk: BytesLike, factor: PasskeyWrapFactor): Promise<PasskeyPrfCopy> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  const nonce = randomBytes(NONCE_BYTES);
  let kek: Uint8Array | undefined;
  let prfSalt: Uint8Array | undefined;
  try {
    assertLength(vmkBytes, KEY_BYTES, 'VMK');
    prfSalt = toUint8Array(factor.prfSalt, 'passkey PRF salt');
    assertLength(prfSalt, PASSKEY_PRF_SALT_BYTES, 'passkey PRF salt');
    kek = await derivePasskeyKek(factor.prfOutput, prfSalt);
    const wrapped = await aesgcmEncrypt(kek, nonce, vmkBytes);
    return {
      kind: 'passkey',
      kdf: 'webauthn-prf-hkdf-sha256',
      prf_salt: bytesToBase64(prfSalt),
      nonce: bytesToBase64(nonce),
      wrapped: bytesToBase64(wrapped),
      ...(factor.credentialId ? { credential_id: factor.credentialId } : {}),
    };
  } finally {
    kek?.fill(0);
    prfSalt?.fill(0);
    vmkBytes.fill(0);
  }
}

export async function buildWrapMeta(vmk: BytesLike, factors: PasskeyWrapFactor[]): Promise<string> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  try {
    assertLength(vmkBytes, KEY_BYTES, 'VMK');
    if (factors.length === 0) {
      throw new Error('at least one protected unlock factor is required');
    }
    const copies: PasskeyPrfCopy[] = [];
    for (const factor of factors) {
      copies.push(await passkeyCopy(vmkBytes, factor));
    }
    return JSON.stringify({ v: 1, copies } satisfies WrapMeta);
  } finally {
    vmkBytes.fill(0);
  }
}

export async function addPasskeyCopy(
  wrapMeta: string,
  vmk: BytesLike,
  prfOutput: BytesLike,
  prfSalt: BytesLike,
  credentialId?: string,
): Promise<string> {
  const meta = parseWrapMeta(wrapMeta);
  meta.copies.push(await passkeyCopy(vmk, { kind: 'passkey', prfOutput, prfSalt, credentialId }));
  return JSON.stringify(meta);
}

async function unwrapPasskeyCopy(copy: PasskeyPrfCopy, factor: PasskeyUnlockFactor): Promise<Uint8Array> {
  const prfSalt = base64ToBytes(copy.prf_salt);
  if (factor.prfSalt && bytesToBase64(factor.prfSalt) !== copy.prf_salt) {
    throw new Error('passkey PRF salt does not match copy');
  }
  const kek = await derivePasskeyKek(factor.prfOutput, prfSalt);
  try {
    return await aesgcmDecrypt(kek, base64ToBytes(copy.nonce), base64ToBytes(copy.wrapped));
  } finally {
    kek.fill(0);
  }
}

export async function unwrapVmk(wrapMeta: string | WrapMeta, factor: PasskeyUnlockFactor): Promise<Uint8Array> {
  const meta = parseWrapMeta(wrapMeta);
  for (const copy of meta.copies) {
    try {
      if (copy.kind === 'passkey') {
        const vmk = await unwrapPasskeyCopy(copy, factor);
        try {
          assertLength(vmk, KEY_BYTES, 'VMK');
          return vmk;
        } catch (error) {
          vmk.fill(0);
          throw error;
        }
      }
    } catch {
      // Wrong factor or corrupt copy; try the next independent VMK copy.
    }
  }
  throw new Error('no protected VMK copy could be unwrapped');
}

export function passkeyPrfSalts(wrapMeta: string | WrapMeta): Uint8Array[] {
  return passkeyPrfSaltEntries(wrapMeta).map((entry) => entry.prfSalt);
}

export function passkeyPrfSaltEntries(wrapMeta: string | WrapMeta): PasskeyPrfSalt[] {
  return parseWrapMeta(wrapMeta)
    .copies.filter((copy): copy is PasskeyPrfCopy => copy.kind === 'passkey')
    .map((copy) => ({
      prfSalt: base64ToBytes(copy.prf_salt),
      ...(copy.credential_id ? { credentialId: copy.credential_id } : {}),
    }));
}

function protectedRecordAad(context: ProtectedRecordContext): Uint8Array {
  const name = utf8(validateSecretName(context.name));
  const scheme = utf8(context.scheme ?? WRAP_SCHEME);
  const out = new Uint8Array(name.length + scheme.length + 1);
  out.set(name, 0);
  out.set(scheme, name.length);
  out[name.length + scheme.length] = context.version ?? WRAP_META_VERSION;
  return out;
}

export function protectedRecordAadHex(context: ProtectedRecordContext): string {
  return bytesToHex(protectedRecordAad(context));
}

function requirePinnedPublicKey(publicKey: AvaultPublicKey): AvaultPublicKey & { fingerprint: string } {
  if (!publicKey.fingerprint) {
    throw new Error('protected DEK release requires a pinned avault public key fingerprint');
  }
  return publicKey as AvaultPublicKey & { fingerprint: string };
}

async function requireValidPinnedPublicKey(publicKey: AvaultPublicKey): Promise<AvaultPublicKey & { fingerprint: string }> {
  const pinned = requirePinnedPublicKey(publicKey);
  const rawPublicKey = publicKeyBytes(pinned);
  assertLength(rawPublicKey, KEY_BYTES, 'avault public key');
  const actual = await publicKeyFingerprint(rawPublicKey);
  if (actual !== pinned.fingerprint.toLowerCase()) {
    throw new Error('avault public key fingerprint mismatch');
  }
  return pinned;
}

function assertReleaseMatchesRecord(recordContext: ProtectedRecordContext, context: ProtectedDekReleaseBlindBoxContext): void {
  if (validateSecretName(recordContext.name) !== validateSecretName(context.name)) {
    throw new Error('protected DEK release context name does not match record name');
  }
}

function assertDeliveryReleaseContext(
  context: ProtectedDekReleaseBlindBoxContext,
): asserts context is ProtectedDekDeliveryBlindBoxContext {
  if (context.purpose === 'agent-sign') {
    throw new Error('protected signing keys must be signed locally; DEK release only supports delivery contexts');
  }
}

export async function sealProtected(
  value: BytesLike,
  vmk: BytesLike,
  context: ProtectedRecordContext,
): Promise<ProtectedSealed> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  const dek = randomBytes(KEY_BYTES);
  let valueBytes: Uint8Array | undefined;
  try {
    assertLength(vmkBytes, KEY_BYTES, 'VMK');
    valueBytes = toUint8Array(value, 'value');
    const aad = protectedRecordAad(context);
    const valueNonce = randomBytes(NONCE_BYTES);
    const ciphertext = await aesgcmEncrypt(dek, valueNonce, valueBytes, aad);
    const dekNonce = randomBytes(NONCE_BYTES);
    const wrappedDek = await aesgcmEncrypt(vmkBytes, dekNonce, dek, aad);
    return {
      ciphertext: bytesToBase64(ciphertext),
      nonce: bytesToBase64(valueNonce),
      dek_nonce: bytesToBase64(dekNonce),
      wrapped_dek: bytesToBase64(wrappedDek),
    };
  } finally {
    dek.fill(0);
    vmkBytes.fill(0);
    valueBytes?.fill(0);
  }
}

export async function unwrapProtectedDek(
  sealed: ProtectedSealed,
  vmk: BytesLike,
  context: ProtectedRecordContext,
): Promise<Uint8Array> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  try {
    assertLength(vmkBytes, KEY_BYTES, 'VMK');
    const dek = await aesgcmDecrypt(
      vmkBytes,
      base64ToBytes(sealed.dek_nonce),
      base64ToBytes(sealed.wrapped_dek),
      protectedRecordAad(context),
    );
    try {
      assertLength(dek, KEY_BYTES, 'DEK');
      return dek;
    } catch (error) {
      dek.fill(0);
      throw error;
    }
  } finally {
    vmkBytes.fill(0);
  }
}

export async function openProtected(
  sealed: ProtectedSealed,
  vmk: BytesLike,
  context: ProtectedRecordContext,
): Promise<Uint8Array> {
  const dek = await unwrapProtectedDek(sealed, vmk, context);
  try {
    return await aesgcmDecrypt(
      dek,
      base64ToBytes(sealed.nonce),
      base64ToBytes(sealed.ciphertext),
      protectedRecordAad(context),
    );
  } finally {
    dek.fill(0);
  }
}

export async function releaseProtectedDek(
  sealed: ProtectedSealed,
  vmk: BytesLike,
  publicKey: AvaultPublicKey,
  recordContext: ProtectedRecordContext,
  context: ProtectedDekDeliveryBlindBoxContext,
): Promise<BlindBox> {
  assertDeliveryReleaseContext(context);
  assertReleaseMatchesRecord(recordContext, context);
  await assertAgentDeliverReleaseHash(context);
  const pinnedPublicKey = await requireValidPinnedPublicKey(publicKey);
  const dek = await unwrapProtectedDek(sealed, vmk, recordContext);
  try {
    return await sealBlindBox(dek, pinnedPublicKey, context);
  } finally {
    dek.fill(0);
  }
}

function hpkeSuite(): CipherSuite {
  return new CipherSuite({
    kem: new DhkemX25519HkdfSha256(),
    kdf: new HkdfSha256(),
    aead: new Aes256Gcm(),
  });
}

function publicKeyBytes(publicKey: AvaultPublicKey | string): Uint8Array {
  return base64ToBytes(typeof publicKey === 'string' ? publicKey : publicKey.public_key);
}

async function publicKeyFingerprint(publicKeyRaw: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', toArrayBuffer(publicKeyRaw));
  return bytesToHex(new Uint8Array(digest));
}

export async function avaultPublicKeyFingerprint(publicKey: AvaultPublicKey | string): Promise<string> {
  return publicKeyFingerprint(publicKeyBytes(publicKey));
}

export async function sealBlindBox(
  plaintext: BytesLike | string,
  publicKey: AvaultPublicKey | string,
  context: BlindBoxContext,
): Promise<BlindBox> {
  const suite = hpkeSuite();
  const publicKeyRaw = publicKeyBytes(publicKey);
  assertLength(publicKeyRaw, KEY_BYTES, 'avault public key');
  if (typeof publicKey !== 'string' && publicKey.fingerprint) {
    const actual = await avaultPublicKeyFingerprint(publicKey);
    if (actual !== publicKey.fingerprint.toLowerCase()) {
      throw new Error('avault public key fingerprint mismatch');
    }
  }

  const recipientPublicKey = await suite.kem.deserializePublicKey(publicKeyRaw);
  const pt = typeof plaintext === 'string' ? utf8(plaintext) : toUint8Array(plaintext, 'plaintext');
  try {
    const sealed = await suite.seal(
      { recipientPublicKey, info: utf8(BLIND_BOX_HPKE_INFO) },
      pt,
      blindBoxAad(context),
    );
    return {
      scheme: BLIND_BOX_SCHEME,
      enc: bytesToBase64(new Uint8Array(sealed.enc)),
      ct: bytesToBase64(new Uint8Array(sealed.ct)),
    };
  } finally {
    pt.fill(0);
  }
}

function normalizeDigest(digest: BytesLike | string): Uint8Array {
  const bytes = typeof digest === 'string' ? hexToBytes(digest) : toUint8Array(digest, 'digest');
  assertLength(bytes, KEY_BYTES, 'digest');
  return bytes;
}

function normalizePrivateKey(privateKey: BytesLike | string): Uint8Array {
  const bytes = typeof privateKey === 'string' ? hexToBytes(privateKey) : toUint8Array(privateKey, 'private key');
  try {
    assertLength(bytes, KEY_BYTES, 'private key');
    if (!secp256k1.utils.isValidSecretKey(bytes)) {
      throw new Error('invalid secp256k1 private key');
    }
    return bytes;
  } catch (error) {
    bytes.fill(0);
    throw error;
  }
}

function normalizeHexOrBytes(value: HexOrBytes, field: string): Uint8Array {
  return typeof value === 'string' ? hexToBytes(value) : toUint8Array(value, field);
}

export type SigningKeyMaterial = {
  /** Raw 32-byte secp256k1 private key. The caller MUST zero it after sealing. */
  privateKey: Uint8Array;
  /**
   * Compressed secp256k1 public key (33 bytes), hex. A signing key is
   * chain-agnostic: the signature scheme (ECDSA / Schnorr) is chosen at sign
   * time, not at creation, so this is the only public material we pin.
   */
  publicKey: string;
};

/** Generate a fresh, chain-agnostic secp256k1 signing key. */
export function generateSigningKey(): SigningKeyMaterial {
  const privateKey = secp256k1.utils.randomSecretKey();
  return { privateKey, publicKey: bytesToHex(secp256k1.getPublicKey(privateKey, true)) };
}

/**
 * Validate + load an imported secp256k1 private key (hex or raw bytes) and
 * derive its compressed public key. Throws if it is not a valid 32-byte key.
 */
export function importSigningKey(privateKey: BytesLike | string): SigningKeyMaterial {
  const key = normalizePrivateKey(privateKey);
  return { privateKey: key, publicKey: bytesToHex(secp256k1.getPublicKey(key, true)) };
}

function validateSecretName(name: string): string {
  if (typeof name !== 'string' || !SECRET_NAME_PATTERN.test(name)) {
    throw new Error('vault secret name must match ^[A-Za-z_][A-Za-z0-9_]*$');
  }
  return name;
}

function validateNonEmpty(value: string, field: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`${field} is required`);
  }
  return value;
}

function normalizeOperationHash(value: HexOrBytes): Uint8Array {
  const bytes = normalizeHexOrBytes(value, 'operation hash');
  assertLength(bytes, KEY_BYTES, 'operation hash');
  return bytes;
}

function normalizeApproval(approval: BlindBoxApproval): {
  approvalNonce: Uint8Array;
  approvalExpiresAtUnix: bigint;
} {
  const approvalNonce = toUint8Array(approval.nonce, 'approval nonce');
  if (approvalNonce.length < 16 || approvalNonce.length > 128) {
    throw new Error('approval nonce must be 16..128 bytes');
  }
  const approvalExpiresAtUnix = normalizeUnixU64(approval.expiresAtUnix);
  return { approvalNonce, approvalExpiresAtUnix };
}

function normalizeU64(value: number | bigint, field: string): bigint {
  if (typeof value === 'number' && (!Number.isSafeInteger(value) || value < 0)) {
    throw new Error(`${field} out of bounds`);
  }
  const normalized = typeof value === 'bigint' ? value : BigInt(value);
  if (normalized < 0n || normalized > 0xffff_ffff_ffff_ffffn) {
    throw new Error(`${field} out of bounds`);
  }
  return normalized;
}

function normalizeUnixU64(value: number | bigint): bigint {
  return normalizeU64(value, 'approval expiry');
}

function u64Be(value: number | bigint, field: string): Uint8Array {
  const normalized = normalizeU64(value, field);
  const out = new Uint8Array(8);
  new DataView(out.buffer).setBigUint64(0, normalized, false);
  return out;
}

function approvalExpiryBytes(value: number | bigint): Uint8Array {
  return u64Be(value, 'approval expiry');
}

function pushLengthPrefixed(out: number[], value: BytesLike): void {
  const bytes = toUint8Array(value);
  if (bytes.length > 0xffff_ffff) {
    throw new Error('blind-box AAD field is too large');
  }
  out.push((bytes.length >>> 24) & 0xff, (bytes.length >>> 16) & 0xff, (bytes.length >>> 8) & 0xff, bytes.length & 0xff);
  out.push(...bytes);
}

function normalizeBlindBoxContext(context: BlindBoxContext): BlindBoxContext {
  switch (context.purpose) {
    case 'seal':
      return standardCreateBlindBoxContext(context.name);
    case 'agent-deliver': {
      const approval = normalizeApproval({
        nonce: context.approvalNonce,
        expiresAtUnix: context.approvalExpiresAtUnix,
      });
      return {
        purpose: 'agent-deliver',
        name: validateSecretName(context.name),
        grantId: validateNonEmpty(context.grantId, 'grant id'),
        ttlSecs: normalizeU64(context.ttlSecs, 'ttl seconds'),
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: normalizeOperationHash(context.operationHash),
      };
    }
    case 'agent-sign': {
      const approval = normalizeApproval({
        nonce: context.approvalNonce,
        expiresAtUnix: context.approvalExpiresAtUnix,
      });
      return {
        purpose: 'agent-sign',
        name: validateSecretName(context.name),
        grantId: validateNonEmpty(context.grantId, 'grant id'),
        signScheme: context.signScheme,
        digest: normalizeDigest(context.digest),
        ttlSecs: normalizeU64(context.ttlSecs, 'ttl seconds'),
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: normalizeOperationHash(context.operationHash),
      };
    }
    default:
      throw new Error('unsupported blind-box context purpose');
  }
}

export function standardCreateBlindBoxContext(name: string): StandardCreateBlindBoxContext {
  return {
    purpose: 'seal',
    name: validateSecretName(name),
  };
}

async function normalizeAndCheckOperationHash(
  expectedFields: BlindBoxOperationHashField[],
  suppliedHash: HexOrBytes,
): Promise<Uint8Array> {
  const supplied = normalizeOperationHash(suppliedHash);
  const computed = await blindBoxOperationHash(expectedFields);
  if (bytesToHex(supplied) !== bytesToHex(computed)) {
    throw new Error('operation hash does not match release operation fields');
  }
  return supplied;
}

async function assertAgentDeliverReleaseHash(context: ProtectedDekDeliveryBlindBoxContext): Promise<void> {
  const supplied = normalizeOperationHash(context.operationHash);
  const computed = await blindBoxAgentDeliverOperationHash(context.name, context.ttlSecs);
  if (bytesToHex(supplied) !== bytesToHex(computed)) {
    throw new Error('operation hash does not match release operation fields');
  }
}

export async function protectedDekReleaseBlindBoxContext(
  name: string,
  operation: ProtectedDekReleaseOperation,
): Promise<ProtectedDekReleaseBlindBoxContext> {
  const normalizedName = validateSecretName(name);
  const approval = normalizeApproval(operation.approval);

  switch (operation.kind) {
    case 'agent-deliver': {
      const ttlSecs = normalizeU64(operation.ttlSecs, 'ttl seconds');
      return {
        purpose: 'agent-deliver',
        name: normalizedName,
        grantId: validateNonEmpty(operation.grantId, 'grant id'),
        ttlSecs,
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: await normalizeAndCheckOperationHash(
          ['agent-deliver', normalizedName, u64Be(ttlSecs, 'ttl seconds')],
          operation.operationHash,
        ),
      };
    }
    case 'agent-sign': {
      const digest = normalizeDigest(operation.digest);
      const ttlSecs = normalizeU64(operation.ttlSecs, 'ttl seconds');
      return {
        purpose: 'agent-sign',
        name: normalizedName,
        grantId: validateNonEmpty(operation.grantId, 'grant id'),
        signScheme: operation.signatureScheme,
        digest,
        ttlSecs,
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: await normalizeAndCheckOperationHash(
          [
            'agent-sign',
            operation.signatureScheme,
            digest,
            u64Be(ttlSecs, 'ttl seconds'),
          ],
          operation.operationHash,
        ),
      };
    }
    default:
      throw new Error('unsupported blind-box release operation');
  }
}

export async function blindBoxOperationHash(fields: BlindBoxOperationHashField[]): Promise<Uint8Array> {
  const encoded: number[] = [];
  for (const field of fields) {
    pushLengthPrefixed(encoded, typeof field === 'string' ? utf8(field) : toUint8Array(field));
  }
  return new Uint8Array(await crypto.subtle.digest('SHA-256', new Uint8Array(encoded)));
}

export async function blindBoxAgentDeliverOperationHash(
  name: string,
  ttlSecs: number | bigint,
): Promise<Uint8Array> {
  return blindBoxOperationHash([
    'agent-deliver',
    validateSecretName(name),
    u64Be(ttlSecs, 'ttl seconds'),
  ]);
}

export async function blindBoxAgentSignOperationHash(
  signScheme: SignatureScheme,
  digest: HexOrBytes,
  ttlSecs: number | bigint,
): Promise<Uint8Array> {
  return blindBoxOperationHash([
    'agent-sign',
    signScheme,
    normalizeDigest(digest),
    u64Be(ttlSecs, 'ttl seconds'),
  ]);
}

export function blindBoxAad(context: BlindBoxContext): Uint8Array {
  const normalized = normalizeBlindBoxContext(context);
  const out: number[] = [...utf8(BLIND_BOX_AAD_DOMAIN)];
  pushLengthPrefixed(out, utf8(normalized.purpose));
  pushLengthPrefixed(out, utf8(normalized.name));
  pushLengthPrefixed(out, utf8(WRAP_SCHEME));
  pushLengthPrefixed(out, new Uint8Array([WRAP_META_VERSION]));
  pushLengthPrefixed(
    out,
    normalized.purpose === 'agent-deliver' || normalized.purpose === 'agent-sign'
      ? utf8(normalized.grantId)
      : new Uint8Array(),
  );
  // Reserved empty field keeps the v1 AAD layout stable while grant_id is the only scope.
  pushLengthPrefixed(out, new Uint8Array());
  pushLengthPrefixed(
    out,
    normalized.purpose === 'agent-sign' ? utf8(normalized.signScheme) : new Uint8Array(),
  );
  pushLengthPrefixed(
    out,
    normalized.purpose === 'agent-sign' ? normalizeDigest(normalized.digest) : new Uint8Array(),
  );
  pushLengthPrefixed(
    out,
    normalized.purpose === 'seal' ? new Uint8Array() : toUint8Array(normalized.approvalNonce, 'approval nonce'),
  );
  pushLengthPrefixed(
    out,
    normalized.purpose === 'seal'
      ? new Uint8Array()
      : approvalExpiryBytes(normalized.approvalExpiresAtUnix),
  );
  pushLengthPrefixed(
    out,
    normalized.purpose === 'seal' ? new Uint8Array() : normalizeOperationHash(normalized.operationHash),
  );
  return new Uint8Array(out);
}

export function blindBoxAadHex(context: BlindBoxContext): string {
  return bytesToHex(blindBoxAad(context));
}

export function signDigest(
  privateKey: HexOrBytes,
  digest: HexOrBytes,
  scheme: SignatureScheme,
  options: SignDigestOptions = {},
): SignatureResult {
  const key = normalizePrivateKey(privateKey);
  try {
    const msg = normalizeDigest(digest);
    if (scheme === SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE) {
      const recovered = secp256k1.sign(msg, key, { prehash: false, lowS: true, format: 'recovered' });
      return {
        scheme,
        signature: bytesToHex(recovered.slice(1)),
        recovery_id: recovered[0] ?? null,
      };
    }
    if (scheme === SIGN_SCHEME_ECDSA_SECP256K1_DER) {
      return {
        scheme,
        signature: bytesToHex(secp256k1.sign(msg, key, { prehash: false, lowS: true, format: 'der' })),
        recovery_id: null,
      };
    }
    if (scheme === SIGN_SCHEME_SCHNORR_SECP256K1_BIP340) {
      const aux = options.schnorrAuxRand
        ? normalizeHexOrBytes(options.schnorrAuxRand, 'schnorr aux randomness')
        : undefined;
      if (aux) {
        assertLength(aux, KEY_BYTES, 'schnorr aux randomness');
      }
      return {
        scheme,
        signature: bytesToHex(schnorr.sign(msg, key, aux)),
        recovery_id: null,
      };
    }
    throw new Error('unsupported signing scheme');
  } finally {
    key.fill(0);
  }
}

export async function signProtectedDigest(
  sealedKey: ProtectedSealed,
  vmk: BytesLike,
  context: ProtectedRecordContext,
  digest: BytesLike | string,
  scheme: SignatureScheme,
  options: SignDigestOptions = {},
): Promise<SignatureResult> {
  const privateKey = await openProtected(sealedKey, vmk, context);
  try {
    return signDigest(privateKey, digest, scheme, options);
  } finally {
    privateKey.fill(0);
  }
}

/**
 * Storage composition for protected-tier records.
 *
 * The daemon stores a protected secret as an opaque `{ciphertext, nonce, wrap_meta}`
 * triple. The browser produces two artifacts on create: the {@link ProtectedSealed}
 * envelope (value encrypted under a per-record DEK, and that DEK wrapped under the
 * VMK) and the VMK `wrap_meta` (the VMK wrapped under the user's factors). We fold the
 * DEK-under-VMK fields into the stored `wrap_meta` so every record is self-contained
 * and round-trippable — pack on create, unpack on open. Field names match the Python
 * reference (`storage/vault_protected.py`).
 */
export type ProtectedRecordEnvelope = { ciphertext: string; nonce: string; wrap_meta: string };

export function packProtectedRecord(sealed: ProtectedSealed, vmkWrapMeta: string): ProtectedRecordEnvelope {
  const meta = JSON.parse(vmkWrapMeta) as Record<string, unknown>;
  if ('dek_nonce' in meta || 'wrapped_dek' in meta) {
    throw new Error('vmk wrap_meta must not already carry a wrapped DEK');
  }
  if ('scheme' in meta && meta.scheme !== WRAP_SCHEME) {
    throw new Error('protected wrap_meta has unsupported scheme');
  }
  return {
    ciphertext: sealed.ciphertext,
    nonce: sealed.nonce,
    wrap_meta: JSON.stringify({ ...meta, scheme: WRAP_SCHEME, dek_nonce: sealed.dek_nonce, wrapped_dek: sealed.wrapped_dek }),
  };
}

export function unpackProtectedRecord(envelope: ProtectedRecordEnvelope): { sealed: ProtectedSealed; vmkWrapMeta: string } {
  const parsed = JSON.parse(envelope.wrap_meta) as Record<string, unknown>;
  const { dek_nonce, wrapped_dek, ...vmkMeta } = parsed;
  if (typeof dek_nonce !== 'string' || typeof wrapped_dek !== 'string') {
    throw new Error('protected record wrap_meta is missing the wrapped DEK');
  }
  return {
    sealed: { ciphertext: envelope.ciphertext, nonce: envelope.nonce, dek_nonce, wrapped_dek },
    vmkWrapMeta: JSON.stringify(vmkMeta),
  };
}
