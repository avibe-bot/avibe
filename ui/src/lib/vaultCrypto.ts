import { Aes256Gcm, CipherSuite, HkdfSha256 } from '@hpke/core';
import { DhkemX25519HkdfSha256 } from '@hpke/dhkem-x25519';
import { secp256k1, schnorr } from '@noble/curves/secp256k1.js';
import { bytesToHex, hexToBytes } from '@noble/curves/utils.js';
import { argon2id, scrypt } from 'hash-wasm';

export const BLIND_BOX_SCHEME = 'hpke-x25519-hkdfsha256-aes256gcm-v1';
export const BLIND_BOX_HPKE_INFO = 'avault:blind-box:v1';
export const SIGN_SCHEME_ECDSA_SECP256K1_RECOVERABLE = 'ecdsa-secp256k1-recoverable';
export const SIGN_SCHEME_ECDSA_SECP256K1_DER = 'ecdsa-secp256k1-der';
export const SIGN_SCHEME_SCHNORR_SECP256K1_BIP340 = 'schnorr-secp256k1-bip340';

const KEY_BYTES = 32;
const NONCE_BYTES = 12;
const ARGON2_VERSION = 19;
const PASSKEY_PRF_SALT_BYTES = 32;
const PASSKEY_HKDF_INFO = 'avault:protected-vmk:kek-passkey:v1';

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

export type Argon2idParams = {
  iterations: number;
  memorySize: number;
  parallelism: number;
};

export type PasswordWrapFactor = {
  kind: 'password';
  password: string;
  argon2id?: Partial<Argon2idParams>;
};

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

function toUint8Array(value: BytesLike, field = 'bytes'): Uint8Array {
  if (value instanceof Uint8Array) {
    return new Uint8Array(value);
  }
  if (value instanceof ArrayBuffer) {
    return new Uint8Array(value);
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

async function aesgcmEncrypt(key: BytesLike, nonce: BytesLike, data: BytesLike): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey('raw', toArrayBuffer(key), 'AES-GCM', false, ['encrypt']);
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: toArrayBuffer(nonce) },
    cryptoKey,
    toArrayBuffer(data),
  );
  return new Uint8Array(ct);
}

async function aesgcmDecrypt(key: BytesLike, nonce: BytesLike, data: BytesLike): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey('raw', toArrayBuffer(key), 'AES-GCM', false, ['decrypt']);
  const pt = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: toArrayBuffer(nonce) },
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

async function passwordCopy(vmk: BytesLike, factor: PasswordWrapFactor): Promise<PasswordArgon2idCopy> {
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
  meta.copies.push(await passwordCopy(vmk, { kind: 'password', password, argon2id: argon2idParams }));
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
  return parseWrapMeta(wrapMeta)
    .copies.filter((copy): copy is PasskeyPrfCopy => copy.kind === 'passkey')
    .map((copy) => base64ToBytes(copy.prf_salt));
}

export async function sealProtected(value: BytesLike, vmk: BytesLike): Promise<ProtectedSealed> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  assertLength(vmkBytes, KEY_BYTES, 'VMK');
  const dek = randomBytes(KEY_BYTES);
  try {
    const valueNonce = randomBytes(NONCE_BYTES);
    const ciphertext = await aesgcmEncrypt(dek, valueNonce, toUint8Array(value, 'value'));
    const dekNonce = randomBytes(NONCE_BYTES);
    const wrappedDek = await aesgcmEncrypt(vmkBytes, dekNonce, dek);
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

export async function unwrapProtectedDek(sealed: ProtectedSealed, vmk: BytesLike): Promise<Uint8Array> {
  const vmkBytes = toUint8Array(vmk, 'VMK');
  assertLength(vmkBytes, KEY_BYTES, 'VMK');
  return aesgcmDecrypt(vmkBytes, base64ToBytes(sealed.dek_nonce), base64ToBytes(sealed.wrapped_dek));
}

export async function openProtected(sealed: ProtectedSealed, vmk: BytesLike): Promise<Uint8Array> {
  const dek = await unwrapProtectedDek(sealed, vmk);
  try {
    return await aesgcmDecrypt(dek, base64ToBytes(sealed.nonce), base64ToBytes(sealed.ciphertext));
  } finally {
    dek.fill(0);
  }
}

export async function releaseProtectedDek(sealed: ProtectedSealed, vmk: BytesLike, publicKey: AvaultPublicKey | string): Promise<BlindBox> {
  const dek = await unwrapProtectedDek(sealed, vmk);
  try {
    return await sealBlindBox(dek, publicKey);
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

export async function sealBlindBox(plaintext: BytesLike | string, publicKey: AvaultPublicKey | string): Promise<BlindBox> {
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
  const sealed = await suite.seal(
    { recipientPublicKey, info: utf8(BLIND_BOX_HPKE_INFO) },
    pt,
    utf8(BLIND_BOX_SCHEME),
  );
  return {
    scheme: BLIND_BOX_SCHEME,
    enc: bytesToBase64(new Uint8Array(sealed.enc)),
    ct: bytesToBase64(new Uint8Array(sealed.ct)),
  };
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

export function signDigest(
  privateKey: HexOrBytes,
  digest: HexOrBytes,
  scheme: SignatureScheme,
  options: SignDigestOptions = {},
): SignatureResult {
  const key = normalizePrivateKey(privateKey);
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
}

export async function signProtectedDigest(
  sealedKey: ProtectedSealed,
  vmk: BytesLike,
  digest: BytesLike | string,
  scheme: SignatureScheme,
  options: SignDigestOptions = {},
): Promise<SignatureResult> {
  const privateKey = await openProtected(sealedKey, vmk);
  try {
    return signDigest(privateKey, digest, scheme, options);
  } finally {
    privateKey.fill(0);
  }
}
