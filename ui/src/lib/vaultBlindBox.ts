import { Aes256Gcm, CipherSuite, HkdfSha256 } from '@hpke/core';
import { DhkemX25519HkdfSha256 } from '@hpke/dhkem-x25519';

export const BLIND_BOX_SCHEME = 'hpke-x25519-hkdfsha256-aes256gcm-v1';
const BLIND_BOX_HPKE_INFO = 'avault:blind-box:v1';
const BLIND_BOX_AAD_DOMAIN = 'avault:blind-box:aad:v1';
const WRAP_SCHEME = 'machine-aesgcm-v1';
const WRAP_META_VERSION = 1;
const KEY_BYTES = 32;

export type AvaultPublicKey = {
  public_key: string;
  fingerprint?: string;
};

export type VaultBlindBox = {
  scheme: typeof BLIND_BOX_SCHEME;
  enc: string;
  ct: string;
};

export type VaultBlindBoxErrorCode = 'aadFieldTooLarge' | 'invalidPublicKey' | 'fingerprintMismatch';

export class VaultBlindBoxError extends Error {
  readonly code: VaultBlindBoxErrorCode;

  constructor(code: VaultBlindBoxErrorCode) {
    super(code);
    this.name = 'VaultBlindBoxError';
    this.code = code;
  }
}

const textEncoder = new TextEncoder();

function utf8(value: string): Uint8Array {
  return textEncoder.encode(value);
}

function bytesToBase64(value: Uint8Array): string {
  let binary = '';
  for (const byte of value) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function base64ToBytes(value: string): Uint8Array {
  try {
    return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
  } catch {
    throw new VaultBlindBoxError('invalidPublicKey');
  }
}

function bytesToHex(value: Uint8Array): string {
  return Array.from(value, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function toArrayBuffer(value: Uint8Array): ArrayBuffer {
  const out = new ArrayBuffer(value.byteLength);
  new Uint8Array(out).set(value);
  return out;
}

function pushLengthPrefixed(out: number[], value: Uint8Array): void {
  if (value.length > 0xffff_ffff) {
    throw new VaultBlindBoxError('aadFieldTooLarge');
  }
  out.push((value.length >>> 24) & 0xff, (value.length >>> 16) & 0xff, (value.length >>> 8) & 0xff, value.length & 0xff);
  out.push(...value);
}

function standardCreateAad(name: string): Uint8Array {
  const out = [...utf8(BLIND_BOX_AAD_DOMAIN)];
  pushLengthPrefixed(out, utf8('seal'));
  pushLengthPrefixed(out, utf8(name));
  pushLengthPrefixed(out, utf8(WRAP_SCHEME));
  pushLengthPrefixed(out, new Uint8Array([WRAP_META_VERSION]));
  pushLengthPrefixed(out, new Uint8Array());
  pushLengthPrefixed(out, new Uint8Array());
  pushLengthPrefixed(out, new Uint8Array());
  pushLengthPrefixed(out, new Uint8Array());
  pushLengthPrefixed(out, new Uint8Array());
  pushLengthPrefixed(out, new Uint8Array(8));
  pushLengthPrefixed(out, new Uint8Array());
  return new Uint8Array(out);
}

function hpkeSuite(): CipherSuite {
  return new CipherSuite({
    kem: new DhkemX25519HkdfSha256(),
    kdf: new HkdfSha256(),
    aead: new Aes256Gcm(),
  });
}

async function publicKeyFingerprint(publicKey: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', toArrayBuffer(publicKey));
  return bytesToHex(new Uint8Array(digest));
}

export async function sealStandardCreateBlindBox(
  name: string,
  value: string,
  publicKey: AvaultPublicKey,
): Promise<VaultBlindBox> {
  const publicKeyRaw = base64ToBytes(publicKey.public_key);
  if (publicKeyRaw.length !== KEY_BYTES) {
    throw new VaultBlindBoxError('invalidPublicKey');
  }
  if (publicKey.fingerprint) {
    const actual = await publicKeyFingerprint(publicKeyRaw);
    if (actual !== publicKey.fingerprint.toLowerCase()) {
      throw new VaultBlindBoxError('fingerprintMismatch');
    }
  }

  const suite = hpkeSuite();
  const recipientPublicKey = await suite.kem.deserializePublicKey(publicKeyRaw);
  const sealed = await suite.seal(
    { recipientPublicKey, info: utf8(BLIND_BOX_HPKE_INFO) },
    utf8(value),
    standardCreateAad(name),
  );
  return {
    scheme: BLIND_BOX_SCHEME,
    enc: bytesToBase64(new Uint8Array(sealed.enc)),
    ct: bytesToBase64(new Uint8Array(sealed.ct)),
  };
}
