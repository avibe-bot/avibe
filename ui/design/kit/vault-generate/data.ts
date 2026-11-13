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
