import { Aes256Gcm, CipherSuite, HkdfSha256 } from '@hpke/core';
import { DhkemX25519HkdfSha256 } from '@hpke/dhkem-x25519';
import { secp256k1, schnorr } from '@noble/curves/secp256k1.js';
import { bytesToHex, hexToBytes } from '@noble/curves/utils.js';
import { argon2id, scrypt } from 'hash-wasm';

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
const ARGON2_VERSION = 19;
const PASSKEY_PRF_SALT_BYTES = 32;
const PASSKEY_HKDF_INFO = 'avault:protected-vmk:kek-passkey:v1';
const DEFAULT_SCRYPT = {
  n: 2 ** 15,
  r: 8,
  p: 1,
} as const;

const DEFAULT_ARGON2ID = {
  iterations: 3,
  memorySize: 64 * 1024,
  parallelism: 1,
} as const;

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

export type Argon2idParams = {
  iterations: number;
  memorySize: number;
  parallelism: number;
};

type PasswordScryptFactor = {
  kind: 'password';
  password: string;
  kdf?: 'scrypt';
};

type PasswordArgon2idFactor = {
  kind: 'password';
  password: string;
  kdf: 'argon2id';
  argon2id?: Partial<Argon2idParams>;
};

export type PasswordWrapFactor = PasswordScryptFactor | PasswordArgon2idFactor;

export type PasskeyWrapFactor = {
  kind: 'passkey';
  prfOutput: BytesLike;
  prfSalt: BytesLike;
  credentialId?: string;
};

export type VmkWrapFactor = PasswordWrapFactor | PasskeyWrapFactor;

export type PasswordUnlockFactor = {
  kind: 'password';
  password: string;
};

export type PasskeyUnlockFactor = {
  kind: 'passkey';
  prfOutput: BytesLike;
  prfSalt?: BytesLike;
};

export type VmkUnlockFactor = PasswordUnlockFactor | PasskeyUnlockFactor;

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

export type ProtectedDeliverBlindBoxContext = {
  purpose: 'deliver';
  name: string;
  approvalNonce: BytesLike;
  approvalExpiresAtUnix: number | bigint;
  operationHash: HexOrBytes;
};

export type ProtectedSignBlindBoxContext = {
  purpose: 'sign';
  name: string;
  signScheme: SignatureScheme;
  digest: HexOrBytes;
  approvalNonce: BytesLike;
  approvalExpiresAtUnix: number | bigint;
  operationHash: HexOrBytes;
};

export type AgentDeliverBlindBoxContext = {
  purpose: 'agent-deliver';
  name: string;
  scopeType: string;
  scopeRef: string;
  approvalNonce: BytesLike;
  approvalExpiresAtUnix: number | bigint;
  operationHash: HexOrBytes;
};

export type AgentSignBlindBoxContext = {
  purpose: 'agent-sign';
  name: string;
  scopeType: string;
  scopeRef: string;
  signScheme: SignatureScheme;
  digest: HexOrBytes;
  approvalNonce: BytesLike;
  approvalExpiresAtUnix: number | bigint;
  operationHash: HexOrBytes;
};

export type ProtectedDekReleaseBlindBoxContext =
  | ProtectedDeliverBlindBoxContext
  | ProtectedSignBlindBoxContext
  | AgentDeliverBlindBoxContext
  | AgentSignBlindBoxContext;

export type BlindBoxContext = StandardCreateBlindBoxContext | ProtectedDekReleaseBlindBoxContext;

export type ProtectedDekReleaseOperation =
  | {
      kind: 'deliver';
      approval: BlindBoxApproval;
      operationHash: HexOrBytes;
    }
  | {
      kind: 'sign';
      signatureScheme: SignatureScheme;
      digest: HexOrBytes;
      approval: BlindBoxApproval;
      operationHash: HexOrBytes;
    }
  | {
      kind: 'agent-deliver';
      scopeType: string;
      scopeRef: string;
      ttlSecs: number | bigint;
      approval: BlindBoxApproval;
      operationHash: HexOrBytes;
    }
  | {
      kind: 'agent-sign';
      scopeType: string;
      scopeRef: string;
      signatureScheme: SignatureScheme;
      digest: HexOrBytes;
      ttlSecs: number | bigint;
      approval: BlindBoxApproval;
      operationHash: HexOrBytes;
    };

type WrapMeta = {
  v: 1;
  copies: WrapCopy[];
};

type PasswordArgon2idCopy = {
  kind: 'password';
  kdf: 'argon2id';
  version: 19;
  iterations: number;
  memorySize: number;
  parallelism: number;
  salt: string;
  nonce: string;
  wrapped: string;
};

type PasswordScryptCopy = {
  kind: 'password';
  kdf: 'scrypt';
  n: number;
  r: number;
  p: number;
  salt: string;
  nonce: string;
  wrapped: string;
};

type PasskeyPrfCopy = {
  kind: 'passkey';
  kdf: 'webauthn-prf-hkdf-sha256';
  prf_salt: string;
  nonce: string;
  wrapped: string;
  credential_id?: string;
};

type WrapCopy = PasswordArgon2idCopy | PasswordScryptCopy | PasskeyPrfCopy;

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

function normalizeArgon2idParams(params?: Partial<Argon2idParams>): Argon2idParams {
  const normalized = { ...DEFAULT_ARGON2ID, ...params };
  if (!Number.isInteger(normalized.iterations) || normalized.iterations < 1 || normalized.iterations > 10) {
    throw new Error('argon2id iterations out of bounds');
  }
  if (!Number.isInteger(normalized.memorySize) || normalized.memorySize < 8 || normalized.memorySize > 256 * 1024) {
    throw new Error('argon2id memorySize out of bounds');
  }
  if (!Number.isInteger(normalized.parallelism) || normalized.parallelism < 1 || normalized.parallelism > 8) {
    throw new Error('argon2id parallelism out of bounds');
  }
  return normalized;
}

function validateScryptParams(n: number, r: number, p: number): void {
  if (!Number.isInteger(n) || n < 2 || (n & (n - 1)) !== 0 || n > 2 ** 17) {
    throw new Error('scrypt N out of bounds');
  }
  if (!Number.isInteger(r) || r < 1 || r > 16) {
    throw new Error('scrypt r out of bounds');
  }
  if (!Number.isInteger(p) || p < 1 || p > 16) {
    throw new Error('scrypt p out of bounds');
  }
}

async function aesgcmEncrypt(
  key: BytesLike,
  nonce: BytesLike,
  data: BytesLike,
  additionalData?: BytesLike,
): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey('raw', toArrayBuffer(key), 'AES-GCM', false, ['encrypt']);
  const ct = await crypto.subtle.encrypt(
    {
      name: 'AES-GCM',
      iv: toArrayBuffer(nonce),
      ...(additionalData ? { additionalData: toArrayBuffer(additionalData) } : {}),
    },
    cryptoKey,
    toArrayBuffer(data),
  );
  return new Uint8Array(ct);
}

async function aesgcmDecrypt(
  key: BytesLike,
  nonce: BytesLike,
  data: BytesLike,
  additionalData?: BytesLike,
): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey('raw', toArrayBuffer(key), 'AES-GCM', false, ['decrypt']);
  const pt = await crypto.subtle.decrypt(
    {
      name: 'AES-GCM',
      iv: toArrayBuffer(nonce),
      ...(additionalData ? { additionalData: toArrayBuffer(additionalData) } : {}),
    },
    cryptoKey,
    toArrayBuffer(data),
  );
  return new Uint8Array(pt);
}

async function hkdfSha256(ikm: BytesLike, salt: BytesLike, info: BytesLike, length: number): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey('raw', toArrayBuffer(ikm), 'HKDF', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: toArrayBuffer(salt),
      info: toArrayBuffer(info),
    },
    key,
    length * 8,
  );
  return new Uint8Array(bits);
}

async function kekPasswordArgon2id(password: string, salt: BytesLike, params?: Partial<Argon2idParams>): Promise<Uint8Array> {
  const normalized = normalizeArgon2idParams(params);
  return (await argon2id({
    password: utf8(password),
    salt: toUint8Array(salt),
    iterations: normalized.iterations,
    memorySize: normalized.memorySize,
    parallelism: normalized.parallelism,
    hashLength: KEY_BYTES,
    outputType: 'binary',
  })) as Uint8Array;
}

async function kekPasswordScrypt(password: string, salt: BytesLike, n: number, r: number, p: number): Promise<Uint8Array> {
  validateScryptParams(n, r, p);
  return (await scrypt({
    password: utf8(password),
    salt: toUint8Array(salt),
    costFactor: n,
    blockSize: r,
    parallelism: p,
    hashLength: KEY_BYTES,
    outputType: 'binary',
  })) as Uint8Array;
}

export async function derivePasskeyKek(prfOutput: BytesLike, prfSalt: BytesLike): Promise<Uint8Array> {
  const output = toUint8Array(prfOutput, 'passkey PRF output');
  const salt = toUint8Array(prfSalt, 'passkey PRF salt');
  assertLength(output, KEY_BYTES, 'passkey PRF output');
  assertLength(salt, PASSKEY_PRF_SALT_BYTES, 'passkey PRF salt');
  return hkdfSha256(output, salt, utf8(PASSKEY_HKDF_INFO), KEY_BYTES);
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

function normalizeWrapFactors(factors: VmkWrapFactor[] | string[]): VmkWrapFactor[] {
  return factors.map((factor) => (typeof factor === 'string' ? { kind: 'password', password: factor } : factor));
}

async function passwordArgon2idCopy(vmk: BytesLike, factor: PasswordArgon2idFactor): Promise<PasswordArgon2idCopy> {
  const salt = randomBytes(16);
  const nonce = randomBytes(NONCE_BYTES);
  const params = normalizeArgon2idParams(factor.argon2id);
  const kek = await kekPasswordArgon2id(factor.password, salt, params);
  try {
    const wrapped = await aesgcmEncrypt(kek, nonce, toUint8Array(vmk, 'VMK'));
    return {
      kind: 'password',
      kdf: 'argon2id',
      version: ARGON2_VERSION,
      iterations: params.iterations,
      memorySize: params.memorySize,
      parallelism: params.parallelism,
      salt: bytesToBase64(salt),
      nonce: bytesToBase64(nonce),
      wrapped: bytesToBase64(wrapped),
    };
  } finally {
    kek.fill(0);
  }
}

async function passwordScryptCopy(vmk: BytesLike, password: string): Promise<PasswordScryptCopy> {
  const salt = randomBytes(16);
  const nonce = randomBytes(NONCE_BYTES);
  const kek = await kekPasswordScrypt(password, salt, DEFAULT_SCRYPT.n, DEFAULT_SCRYPT.r, DEFAULT_SCRYPT.p);
  try {
    const wrapped = await aesgcmEncrypt(kek, nonce, toUint8Array(vmk, 'VMK'));
    return {
      kind: 'password',
      kdf: 'scrypt',
      n: DEFAULT_SCRYPT.n,
      r: DEFAULT_SCRYPT.r,
      p: DEFAULT_SCRYPT.p,
      salt: bytesToBase64(salt),
      nonce: bytesToBase64(nonce),
      wrapped: bytesToBase64(wrapped),
    };
  } finally {
    kek.fill(0);
  }
}

async function passwordCopy(vmk: BytesLike, factor: PasswordWrapFactor): Promise<PasswordArgon2idCopy | PasswordScryptCopy> {
  return factor.kdf === 'argon2id'
    ? passwordArgon2idCopy(vmk, factor)
    : passwordScryptCopy(vmk, factor.password);
}

async function passkeyCopy(vmk: BytesLike, factor: PasskeyWrapFactor): Promise<PasskeyPrfCopy> {
  const prfSalt = toUint8Array(factor.prfSalt, 'passkey PRF salt');
  assertLength(prfSalt, PASSKEY_PRF_SALT_BYTES, 'passkey PRF salt');
  const nonce = randomBytes(NONCE_BYTES);
  const kek = await derivePasskeyKek(factor.prfOutput, prfSalt);
  try {
    const wrapped = await aesgcmEncrypt(kek, nonce, toUint8Array(vmk, 'VMK'));
    return {
      kind: 'passkey',
      kdf: 'webauthn-prf-hkdf-sha256',
      prf_salt: bytesToBase64(prfSalt),
      nonce: bytesToBase64(nonce),
      wrapped: bytesToBase64(wrapped),
      ...(factor.credentialId ? { credential_id: factor.credentialId } : {}),
    };
  } finally {
    kek.fill(0);
  }
}

export async function buildWrapMeta(vmk: BytesLike, factors: VmkWrapFactor[] | string[]): Promise<string> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  assertLength(vmkBytes, KEY_BYTES, 'VMK');
  const normalized = normalizeWrapFactors(factors);
  if (normalized.length === 0) {
    throw new Error('at least one protected unlock factor is required');
  }

  const copies: WrapCopy[] = [];
  for (const factor of normalized) {
    copies.push(factor.kind === 'password' ? await passwordCopy(vmkBytes, factor) : await passkeyCopy(vmkBytes, factor));
  }
  return JSON.stringify({ v: 1, copies } satisfies WrapMeta);
}

export async function addPasswordCopy(
  wrapMeta: string,
  vmk: BytesLike,
  password: string,
  argon2idParams?: Partial<Argon2idParams>,
): Promise<string> {
  const meta = parseWrapMeta(wrapMeta);
  meta.copies.push(
    await passwordCopy(vmk, {
      kind: 'password',
      password,
      ...(argon2idParams ? { kdf: 'argon2id', argon2id: argon2idParams } : {}),
    }),
  );
  return JSON.stringify(meta);
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

async function unwrapPasswordCopy(copy: PasswordArgon2idCopy | PasswordScryptCopy, password: string): Promise<Uint8Array> {
  const salt = base64ToBytes(copy.salt);
  const nonce = base64ToBytes(copy.nonce);
  const wrapped = base64ToBytes(copy.wrapped);
  const kek =
    copy.kdf === 'argon2id'
      ? await kekPasswordArgon2id(password, salt, {
          iterations: copy.iterations,
          memorySize: copy.memorySize,
          parallelism: copy.parallelism,
        })
      : await kekPasswordScrypt(password, salt, copy.n, copy.r, copy.p);
  try {
    return await aesgcmDecrypt(kek, nonce, wrapped);
  } finally {
    kek.fill(0);
  }
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

export async function unwrapVmk(wrapMeta: string | WrapMeta, factor: VmkUnlockFactor | string): Promise<Uint8Array> {
  const meta = parseWrapMeta(wrapMeta);
  const normalized: VmkUnlockFactor = typeof factor === 'string' ? { kind: 'password', password: factor } : factor;
  for (const copy of meta.copies) {
    try {
      if (normalized.kind === 'password' && copy.kind === 'password') {
        return await unwrapPasswordCopy(copy, normalized.password);
      }
      if (normalized.kind === 'passkey' && copy.kind === 'passkey') {
        return await unwrapPasskeyCopy(copy, normalized);
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
  const out: number[] = [];
  pushLengthPrefixed(out, utf8(validateSecretName(context.name)));
  pushLengthPrefixed(out, utf8(context.scheme ?? WRAP_SCHEME));
  pushLengthPrefixed(out, new Uint8Array([context.version ?? WRAP_META_VERSION]));
  return new Uint8Array(out);
}

export function protectedRecordAadHex(context: ProtectedRecordContext): string {
  return bytesToHex(protectedRecordAad(context));
}

function requirePinnedPublicKey(publicKey: AvaultPublicKey): AvaultPublicKey {
  if (!publicKey.fingerprint) {
    throw new Error('protected DEK release requires a pinned avault public key fingerprint');
  }
  return publicKey;
}

function assertReleaseMatchesRecord(recordContext: ProtectedRecordContext, context: ProtectedDekReleaseBlindBoxContext): void {
  if (validateSecretName(recordContext.name) !== validateSecretName(context.name)) {
    throw new Error('protected DEK release context name does not match record name');
  }
}

export async function sealProtected(
  value: BytesLike,
  vmk: BytesLike,
  context: ProtectedRecordContext,
): Promise<ProtectedSealed> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  assertLength(vmkBytes, KEY_BYTES, 'VMK');
  const aad = protectedRecordAad(context);
  const dek = randomBytes(KEY_BYTES);
  try {
    const valueNonce = randomBytes(NONCE_BYTES);
    const ciphertext = await aesgcmEncrypt(dek, valueNonce, toUint8Array(value, 'value'), aad);
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
  }
}

export async function unwrapProtectedDek(
  sealed: ProtectedSealed,
  vmk: BytesLike,
  context: ProtectedRecordContext,
): Promise<Uint8Array> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  assertLength(vmkBytes, KEY_BYTES, 'VMK');
  return aesgcmDecrypt(
    vmkBytes,
    base64ToBytes(sealed.dek_nonce),
    base64ToBytes(sealed.wrapped_dek),
    protectedRecordAad(context),
  );
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
  context: ProtectedDekReleaseBlindBoxContext,
): Promise<BlindBox> {
  assertReleaseMatchesRecord(recordContext, context);
  const dek = await unwrapProtectedDek(sealed, vmk, recordContext);
  try {
    return await sealBlindBox(dek, requirePinnedPublicKey(publicKey), context);
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

export async function avaultPublicKeyFingerprint(publicKey: AvaultPublicKey | string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', toArrayBuffer(publicKeyBytes(publicKey)));
  return bytesToHex(new Uint8Array(digest));
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
  assertLength(bytes, KEY_BYTES, 'private key');
  if (!secp256k1.utils.isValidSecretKey(bytes)) {
    throw new Error('invalid secp256k1 private key');
  }
  return bytes;
}

function normalizeHexOrBytes(value: HexOrBytes, field: string): Uint8Array {
  return typeof value === 'string' ? hexToBytes(value) : toUint8Array(value, field);
}

function validateSecretName(name: string): string {
  if (typeof name !== 'string' || name.trim() === '') {
    throw new Error('blind-box context name is required');
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
    case 'deliver': {
      const approval = normalizeApproval({
        nonce: context.approvalNonce,
        expiresAtUnix: context.approvalExpiresAtUnix,
      });
      return {
        purpose: 'deliver',
        name: validateSecretName(context.name),
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: normalizeOperationHash(context.operationHash),
      };
    }
    case 'sign': {
      const approval = normalizeApproval({
        nonce: context.approvalNonce,
        expiresAtUnix: context.approvalExpiresAtUnix,
      });
      return {
        purpose: 'sign',
        name: validateSecretName(context.name),
        signScheme: context.signScheme,
        digest: normalizeDigest(context.digest),
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: normalizeOperationHash(context.operationHash),
      };
    }
    case 'agent-deliver': {
      const approval = normalizeApproval({
        nonce: context.approvalNonce,
        expiresAtUnix: context.approvalExpiresAtUnix,
      });
      return {
        purpose: 'agent-deliver',
        name: validateSecretName(context.name),
        scopeType: validateNonEmpty(context.scopeType, 'scope type'),
        scopeRef: validateNonEmpty(context.scopeRef, 'scope ref'),
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
        scopeType: validateNonEmpty(context.scopeType, 'scope type'),
        scopeRef: validateNonEmpty(context.scopeRef, 'scope ref'),
        signScheme: context.signScheme,
        digest: normalizeDigest(context.digest),
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

export async function protectedDekReleaseBlindBoxContext(
  name: string,
  operation: ProtectedDekReleaseOperation,
): Promise<ProtectedDekReleaseBlindBoxContext> {
  const normalizedName = validateSecretName(name);
  const approval = normalizeApproval(operation.approval);

  switch (operation.kind) {
    case 'deliver':
      return {
        purpose: 'deliver',
        name: normalizedName,
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: normalizeOperationHash(operation.operationHash),
      };
    case 'sign':
      return {
        purpose: 'sign',
        name: normalizedName,
        signScheme: operation.signatureScheme,
        digest: normalizeDigest(operation.digest),
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: await normalizeAndCheckOperationHash(
          ['sign', operation.signatureScheme, normalizeDigest(operation.digest)],
          operation.operationHash,
        ),
      };
    case 'agent-deliver':
      return {
        purpose: 'agent-deliver',
        name: normalizedName,
        scopeType: validateNonEmpty(operation.scopeType, 'scope type'),
        scopeRef: validateNonEmpty(operation.scopeRef, 'scope ref'),
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: await normalizeAndCheckOperationHash(
          ['agent-deliver', normalizedName, u64Be(operation.ttlSecs, 'ttl seconds')],
          operation.operationHash,
        ),
      };
    case 'agent-sign':
      return {
        purpose: 'agent-sign',
        name: normalizedName,
        scopeType: validateNonEmpty(operation.scopeType, 'scope type'),
        scopeRef: validateNonEmpty(operation.scopeRef, 'scope ref'),
        signScheme: operation.signatureScheme,
        digest: normalizeDigest(operation.digest),
        approvalNonce: approval.approvalNonce,
        approvalExpiresAtUnix: approval.approvalExpiresAtUnix,
        operationHash: await normalizeAndCheckOperationHash(
          [
            'agent-sign',
            operation.signatureScheme,
            normalizeDigest(operation.digest),
            u64Be(operation.ttlSecs, 'ttl seconds'),
          ],
          operation.operationHash,
        ),
      };
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

export async function blindBoxSignOperationHash(signScheme: SignatureScheme, digest: HexOrBytes): Promise<Uint8Array> {
  return blindBoxOperationHash(['sign', signScheme, normalizeDigest(digest)]);
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
      ? utf8(normalized.scopeType)
      : new Uint8Array(),
  );
  pushLengthPrefixed(
    out,
    normalized.purpose === 'agent-deliver' || normalized.purpose === 'agent-sign'
      ? utf8(normalized.scopeRef)
      : new Uint8Array(),
  );
  pushLengthPrefixed(
    out,
    normalized.purpose === 'sign' || normalized.purpose === 'agent-sign'
      ? utf8(normalized.signScheme)
      : new Uint8Array(),
  );
  pushLengthPrefixed(
    out,
    normalized.purpose === 'sign' || normalized.purpose === 'agent-sign'
      ? normalizeDigest(normalized.digest)
      : new Uint8Array(),
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
