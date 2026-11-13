// Fixed values for the generate-secret proposal (avibe#3338). Each came from a CSPRNG once, as a
// sample of its format; none is a credential.
export type GenFormat = 'password' | 'hex' | 'base64url';

export const SAMPLE = {
  password: 'lTAc!i.WQ&AY:T+%_&^zVJHc',
  password2: 'jZ:b7htaoIw_W!~Y&-BXG$0c',
  hex: '7961a5a1e881ffb639503c82b2329197df5b5696e897560709a4f784460950b5',
  base64url: 'kZEUe8S_pHZaBQDL9onZyIMn410TNwtzlYxA6buvUbk',
} as const;

/** The symbols a generated password may use: safe in env files, shells and URLs (no quotes,
 *  backslash, space, comma or backtick). */
export const SYMBOLS = '!#$%&*+-.:=?@^_~';

/** Options as one recipe. Password counts characters; hex and base64url count random bytes. */
export type Recipe =
  | { format: 'password'; length: number; lower: boolean; upper: boolean; digits: boolean; symbols: boolean; noLookalike: boolean }
  | { format: 'hex' | 'base64url'; bytes: number };

export const DEFAULT_PASSWORD: Recipe = { format: 'password', length: 24, lower: true, upper: true, digits: true, symbols: true, noLookalike: false };
export const HEX_32: Recipe = { format: 'hex', bytes: 32 };
export const B64_32: Recipe = { format: 'base64url', bytes: 32 };

/** Characters a byte recipe produces: hex 2 per byte, base64url ceil(4n/3) without padding. */
export const outputLength = (r: Recipe) => r.format === 'password' ? r.length : r.format === 'hex' ? r.bytes * 2 : Math.ceil((r.bytes * 4) / 3);

// ---- Version 2 (owner 2026-11-13 20:58): the generator is a popup attached to the value field.
// Letters are always in a password; digits, symbols and "no look-alikes" are switches. Base64 is
// the standard alphabet with padding, byte for byte what `openssl rand -base64 <bytes>` prints.
export type GenType = 'password' | 'hex' | 'base64';
export type Gen =
  | { type: 'password'; length: number; digits: boolean; symbols: boolean; noLookalike: boolean }
  | { type: 'hex' | 'base64'; bytes: number };

export const PASSWORD_24: Gen = { type: 'password', length: 24, digits: true, symbols: true, noLookalike: false };
export const HEX32: Gen = { type: 'hex', bytes: 32 };
export const BASE64_32: Gen = { type: 'base64', bytes: 32 };

export const SAMPLE_BASE64 = 'gOIbZqpdW8gRVXMTlcryFE5kTiu9LbHIcoyvWzgxUko=';

/** Characters the value has: hex 2 per byte; Base64 4 per started 3 bytes, padding included. */
export const genChars = (g: Gen) => (g.type === 'password' ? g.length : g.type === 'hex' ? g.bytes * 2 : 4 * Math.ceil(g.bytes / 3));

/** Bits of randomness: length x log2(alphabet) for a password, 8 per byte otherwise. */
export function genBits(g: Gen): number {
  if (g.type !== 'password') return g.bytes * 8;
  const alphabet = 52 + (g.digits ? 10 : 0) + (g.symbols ? SYMBOLS.length : 0) - (g.noLookalike ? (g.digits ? 5 : 3) : 0);
  return Math.floor(g.length * Math.log2(alphabet));
}

// Version 3: a 40-character password, for the frame where the slider is being dragged to 40.
export const SAMPLE_40 = '3pLyrRG^L8?=gnMDixe6q=~WZcYz6d+@v+v35BO8';
export const PASSWORD_40: Gen = { ...PASSWORD_24, length: 40 } as Gen;
