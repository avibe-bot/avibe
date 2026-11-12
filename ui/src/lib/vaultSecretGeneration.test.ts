import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  generateStaticSecret,
  MAX_GENERATED_STATIC_SECRET_LENGTH,
  normalizeStaticSecretGenerationRecipe,
} from './vaultSecretGeneration';

afterEach(() => vi.restoreAllMocks());

describe('static Vault secret generation', () => {
  it('normalizes recipes and enforces strict bounds and value-free fields', () => {
    expect(normalizeStaticSecretGenerationRecipe({ length: 64, charset: 'hex' })).toEqual({ length: 64, charset: 'hex' });
    expect(normalizeStaticSecretGenerationRecipe({
      length: MAX_GENERATED_STATIC_SECRET_LENGTH,
      charset: 'custom',
      lowercase: true,
      digits: true,
      exclude_lookalikes: true,
    })).toEqual({
      length: MAX_GENERATED_STATIC_SECRET_LENGTH,
      charset: 'custom',
      lowercase: true,
      uppercase: false,
      digits: true,
      symbols: false,
      exclude_lookalikes: true,
    });

    for (const invalid of [
      null,
      [],
      { length: 0, charset: 'hex' },
      { length: 513, charset: 'hex' },
      { length: true, charset: 'hex' },
      { length: 4, charset: 'custom' },
      { length: 4, charset: 'custom', digits: 1 },
      { length: 4, charset: 'base64url', lowercase: true },
      { length: 4, charset: 'hex', exclude_lookalikes: true },
      { length: 4, charset: 'hex', value: 'plaintext' },
    ]) {
      expect(() => normalizeStaticSecretGenerationRecipe(invalid)).toThrow();
    }
  });

  it('uses exact preset alphabets and configured custom groups', () => {
    expect(generateStaticSecret({ length: 128, charset: 'hex' })).toMatch(/^[0-9a-f]{128}$/);
    expect(generateStaticSecret({ length: 128, charset: 'base64url' })).toMatch(/^[A-Za-z0-9_-]{128}$/);
    const custom = generateStaticSecret({
      length: 64,
      charset: 'custom',
      lowercase: true,
      uppercase: true,
      digits: true,
      symbols: true,
      exclude_lookalikes: true,
    });
    expect(custom).toHaveLength(64);
    expect(custom).not.toMatch(/[0O1lI|]/);
  });

  it('rejects out-of-range random bytes before modulo selection', () => {
    const random = vi.spyOn(crypto, 'getRandomValues').mockImplementation((target) => {
      const bytes = new Uint8Array(target.buffer, target.byteOffset, target.byteLength);
      bytes.fill(0);
      bytes.set([255, 254, 0, 1]);
      return target;
    });
    expect(generateStaticSecret({ length: 2, charset: 'custom', digits: true })).toBe('01');
    expect(random).toHaveBeenCalledOnce();
  });
});
