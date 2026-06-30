import { describe, expect, it } from 'vitest';

import { readPasskeyPrfResult } from './useProtectedVault';

function passkeyCredential(first?: ArrayBuffer | ArrayBufferView): PublicKeyCredential {
  return {
    getClientExtensionResults: () => ({
      prf: {
        results: first ? { first } : {},
      },
    }),
  } as unknown as PublicKeyCredential;
}

describe('protected vault WebAuthn PRF result handling', () => {
  it('copies the PRF output before handing it to the VMK wrap chain', () => {
    const source = new Uint8Array(32).fill(0x22);
    const backing = source.buffer as ArrayBuffer;
    const result = readPasskeyPrfResult(passkeyCredential(backing));

    structuredClone(backing, { transfer: [backing] });

    expect(result.byteLength).toBe(32);
    expect(result).toEqual(new Uint8Array(32).fill(0x22));
  });

  it('rejects a present but empty PRF output at the WebAuthn boundary', () => {
    expect(() => readPasskeyPrfResult(passkeyCredential(new ArrayBuffer(0)))).toThrow(/passkey-prf-unavailable/);
  });
});
