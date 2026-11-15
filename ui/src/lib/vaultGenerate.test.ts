import { webcrypto } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_PASSWORD_RECIPE,
  VAULT_GENERATE_SYMBOLS,
  generateSecretValue,
  isValidRecipe,
  outputLength,
  recipeBits,
  recipeFromSpec,
  withSize,
  type GenerateRecipe,
  type RandomSource,
} from './vaultGenerate';

/** A random source that hands out `bytes` in order, cycling. */
const fixed = (bytes: number[]): RandomSource => {
  let i = 0;
  return (out) => {
    for (let k = 0; k < out.length; k++) out[k] = bytes[i++ % bytes.length];
  };
};

describe('vaultGenerate', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('uses crypto.getRandomValues and nothing else', () => {
    vi.stubGlobal('crypto', webcrypto);
    const spy = vi.spyOn(webcrypto, 'getRandomValues');
    const random = vi.spyOn(Math, 'random');
    generateSecretValue(DEFAULT_PASSWORD_RECIPE);
    generateSecretValue({ type: 'hex', bytes: 32 });
    expect(spy).toHaveBeenCalled();
    expect(random).not.toHaveBeenCalled();
  });

  it('throws away bytes that would bias a character and keeps the rest uniform', () => {
    // A 64-character alphabet: 256 % 64 = 0, nothing to reject. A 10-character one (digits only is
    // not offered, so use hex-length arithmetic through a password of letters+digits = 62): bytes
    // 248..255 are at or above 256 - 256 % 62 = 248 and must be skipped.
    const recipe: GenerateRecipe = { type: 'password', length: 12, digits: true, symbols: false, noLookalikes: false };
    const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    // 255 and 248 are rejected; 0, 26, 52 map to a, A, 0 and then cycle.
    const value = generateSecretValue(recipe, fixed([255, 0, 248, 26, 52]));
    expect(value).toBe('aA0aA0aA0aA0');
    expect([...value].every((c) => alphabet.includes(c))).toBe(true);
  });

  it('maps every accepted byte to each character equally often', () => {
    const recipe: GenerateRecipe = { type: 'password', length: 62, digits: true, symbols: false, noLookalikes: false };
    // Bytes 0..247 once each: exactly 4 of each of the 62 characters, so with the 248+ bytes thrown
    // away every character comes out the same number of times.
    const bytes = Array.from({ length: 256 }, (_, i) => i);
    const value = [0, 62, 124, 186].map((start) => generateSecretValue(recipe, fixed(bytes.slice(start)))).join('');
    const counts = new Map<string, number>();
    for (const c of value) counts.set(c, (counts.get(c) ?? 0) + 1);
    expect(new Set(counts.values())).toEqual(new Set([4]));
  });

  it('draws the whole string again when a chosen class is missing', () => {
    const recipe: GenerateRecipe = { type: 'password', length: 12, digits: true, symbols: true, noLookalikes: false };
    // The generator draws 24 bytes for 12 characters. The first draw gives twelve 'a' (byte 0): no
    // upper, digit or symbol, so the whole string is drawn again from the next 24 bytes.
    expect(26 + 26 + 10 + VAULT_GENERATE_SYMBOLS.length).toBe(78);
    const second = [26, 52, 62, 1, 2, 3, 4, 5, 6, 7, 8, 9];
    const value = generateSecretValue(recipe, fixed([...Array(24).fill(0), ...second, ...Array(12).fill(0)]));
    expect(value).toBe('A0!bcdefghij');
  });

  it('keeps every class present and look-alikes out', () => {
    vi.stubGlobal('crypto', webcrypto);
    for (let n = 0; n < 200; n++) {
      const value = generateSecretValue({ type: 'password', length: 12, digits: true, symbols: true, noLookalikes: true });
      expect(value).toHaveLength(12);
      expect(value).toMatch(/[a-z]/);
      expect(value).toMatch(/[A-Z]/);
      expect(value).toMatch(/[0-9]/);
      expect([...value].some((c) => VAULT_GENERATE_SYMBOLS.includes(c))).toBe(true);
      expect(value).not.toMatch(/[0O1lI]/);
    }
  });

  it('makes lowercase hex and standard padded Base64 of the drawn bytes', () => {
    const bytes = Array.from({ length: 16 }, (_, i) => i * 17);
    expect(generateSecretValue({ type: 'hex', bytes: 16 }, fixed(bytes))).toBe(Buffer.from(bytes).toString('hex'));
    const b64 = generateSecretValue({ type: 'base64', bytes: 16 }, fixed(bytes));
    expect(b64).toBe(Buffer.from(bytes).toString('base64'));
    expect(b64.endsWith('==')).toBe(true);
    expect(outputLength({ type: 'base64', bytes: 32 })).toBe(44);
    expect(outputLength({ type: 'hex', bytes: 32 })).toBe(64);
  });

  it('holds the limits and refuses anything outside them', () => {
    // Passwords are 8 to 100 characters; hex and Base64 stay 16 to 64 bytes.
    const length = (n: number) => ({ ...DEFAULT_PASSWORD_RECIPE, length: n }) as GenerateRecipe;
    expect([7, 8, 11, 12, 24, 100, 101, 128].map((n) => isValidRecipe(length(n))))
      .toEqual([false, true, true, true, true, true, false, false]);
    expect([7, 8, 100, 101, 128].map((n) => withSize(DEFAULT_PASSWORD_RECIPE, n)))
      .toMatchObject([{ length: 8 }, { length: 8 }, { length: 100 }, { length: 100 }, { length: 100 }]);
    expect(generateSecretValue(length(8))).toHaveLength(8);
    expect(generateSecretValue(length(100))).toHaveLength(100);
    expect(() => generateSecretValue(length(101))).toThrow(RangeError);
    expect(withSize({ type: 'hex', bytes: 32 }, 15.6)).toMatchObject({ bytes: 16 });
    expect(withSize({ type: 'hex', bytes: 32 }, 100)).toMatchObject({ bytes: 64 });
    expect(() => generateSecretValue({ type: 'base64', bytes: 65 })).toThrow(RangeError);
    expect(recipeBits(DEFAULT_PASSWORD_RECIPE)).toBe(150);
    expect(recipeBits({ type: 'hex', bytes: 32 })).toBe(256);
  });

  it('reads a request hint as a preset, bringing an older wider password length within the limits', () => {
    expect(recipeFromSpec({ type: 'hex', bytes: 32, field: 'secret' })).toEqual({ recipe: { type: 'hex', bytes: 32 }, adjusted: false });
    expect(recipeFromSpec({ type: 'password', length: 30, symbols: false, no_lookalikes: true })).toEqual({
      recipe: { type: 'password', length: 30, digits: true, symbols: false, noLookalikes: true }, adjusted: false,
    });
    expect(recipeFromSpec({ type: 'password' })).toEqual({ recipe: DEFAULT_PASSWORD_RECIPE, adjusted: false });
    expect(recipeFromSpec({ type: 'password', length: 8 })).toMatchObject({ recipe: { length: 8 }, adjusted: false });
    // Saved while the range went to 128: kept as an Agent preset, at 100, and marked adjusted.
    expect(recipeFromSpec({ type: 'password', length: 128, symbols: false }))
      .toEqual({ recipe: { type: 'password', length: 100, digits: true, symbols: false, noLookalikes: false }, adjusted: true });
    expect(recipeFromSpec({ type: 'password', length: 12.5 })).toBeNull();
    expect(recipeFromSpec({ type: 'hex', bytes: 65 })).toBeNull();
    expect(recipeFromSpec({ type: 'base64url', bytes: 32 })).toBeNull();
    expect(recipeFromSpec(null)).toBeNull();
  });
});
