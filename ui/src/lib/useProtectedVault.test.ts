import { describe, expect, it } from 'vitest';

import { bytesToBase64 } from './vaultCrypto';
import { passkeyAssertionOptionsFromServer, passkeyCreationOptions, passkeyPrfAssertionOptions, readPasskeyPrfResult } from './useProtectedVault';

function passkeyCredential(first?: ArrayBuffer | ArrayBufferView | number[]): PublicKeyCredential {
  return {
    getClientExtensionResults: () => ({
      prf: {
        results: first ? { first } : {},
      },
    }),
  } as unknown as PublicKeyCredential;
}

describe('protected vault WebAuthn PRF result handling', () => {
  it('creates passkeys as required resident credentials', () => {
    const options = passkeyCreationOptions('alex-app.avibe.bot');

    expect(options.rp.id).toBe('alex-app.avibe.bot');
    expect(options.authenticatorSelection).toEqual({ residentKey: 'required', userVerification: 'required' });
    expect(options.extensions).toEqual({ prf: {} });
  });

  it('uses simple PRF eval with allowCredentials for credential-bound unlock', () => {
    const prfSalt = new Uint8Array(32).fill(0x11);
    const credentialId = new Uint8Array([1, 2, 3, 4]);
    const options = passkeyPrfAssertionOptions(
      [{ credentialId: bytesToBase64(credentialId), prfSalt }],
      'alex-app.avibe.bot',
    );
    const prf = (options.extensions as { prf: { eval: { first: ArrayBuffer }; evalByCredential?: unknown } }).prf;

    expect(options.rpId).toBe('alex-app.avibe.bot');
    expect(options.userVerification).toBe('required');
    expect(options.allowCredentials).toHaveLength(1);
    expect(new Uint8Array(options.allowCredentials?.[0]?.id as ArrayBuffer)).toEqual(credentialId);
    expect(new Uint8Array(prf.eval.first)).toEqual(prfSalt);
    expect(prf.evalByCredential).toBeUndefined();
  });

  it('rejects multiple passkey entries instead of falling back to evalByCredential', () => {
    const prfSalt = new Uint8Array(32).fill(0x11);

    expect(() =>
      passkeyPrfAssertionOptions(
        [
          { credentialId: bytesToBase64(new Uint8Array([1])), prfSalt },
          { credentialId: bytesToBase64(new Uint8Array([2])), prfSalt },
        ],
        'alex-app.avibe.bot',
      ),
    ).toThrow(/passkey-multiple-not-supported/);
  });

  it('builds delete assertion options from the server challenge without leaking factor ids to WebAuthn', () => {
    const challenge = new Uint8Array(32).fill(0x44);
    const credentialId = new Uint8Array([5, 6, 7, 8]);
    const options = passkeyAssertionOptionsFromServer({
      challenge: bytesToBase64(challenge),
      rpId: 'alex-app.avibe.bot',
      userVerification: 'required',
      allowCredentials: [
        {
          type: 'public-key',
          id: bytesToBase64(credentialId),
          factor_id: 'vaf_test',
          transports: ['internal'],
        },
      ],
    });

    expect(new Uint8Array(options.challenge as ArrayBuffer)).toEqual(challenge);
    expect(options.rpId).toBe('alex-app.avibe.bot');
    expect(options.userVerification).toBe('required');
    expect(options.allowCredentials).toEqual([
      {
        type: 'public-key',
        id: options.allowCredentials?.[0]?.id,
        transports: ['internal'],
      },
    ]);
    expect(new Uint8Array(options.allowCredentials?.[0]?.id as ArrayBuffer)).toEqual(credentialId);
    expect('factor_id' in (options.allowCredentials?.[0] as Record<string, unknown>)).toBe(false);
  });

  it('copies the PRF output before handing it to the VMK wrap chain', () => {
    const source = new Uint8Array(32).fill(0x22);
    const backing = source.buffer as ArrayBuffer;
    const result = readPasskeyPrfResult(passkeyCredential(backing));

    structuredClone(backing, { transfer: [backing] });

    expect(result.byteLength).toBe(32);
    expect(result).toEqual(new Uint8Array(32).fill(0x22));
  });

  it('accepts a plain Array PRF output from browser extension passkey providers', () => {
    const source = Array.from({ length: 32 }, (_, index) => index + 1);

    expect(readPasskeyPrfResult(passkeyCredential(source))).toEqual(Uint8Array.from(source));
  });

  it('rejects a present but empty PRF output at the WebAuthn boundary', () => {
    expect(() => readPasskeyPrfResult(passkeyCredential(new ArrayBuffer(0)))).toThrow(/passkey-prf-unavailable/);
  });
});
