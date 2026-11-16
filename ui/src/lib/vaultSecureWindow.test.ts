// The secure-window seal's answer is checked against what was asked (contract v1.1): the same
// records in order, an envelope for every required field, and nothing else trusted.
import { describe, expect, it } from 'vitest';

import { parseSecureWindowSeal, type SecureWindowSealField } from './vaultSandboxClient';

const envelope = { ciphertext: 'c', nonce: 'n', wrap_meta: 'w' };
const asked: SecureWindowSealField[] = [
  { record: 'LOGIN', label: 'password', required: false },
  { record: 'LOGIN.totp', label: 'totp', required: true },
];

describe('parseSecureWindowSeal', () => {
  it('accepts the records in order; a non-required field may come back without an envelope (keep it)', () => {
    const reply = { fields: [{ record: 'LOGIN', generated: false }, { record: 'LOGIN.totp', envelope: { ...envelope, extra: 1 }, generated: true }], establishingVmk: false };
    expect(parseSecureWindowSeal(asked, reply)).toEqual({
      fields: [{ record: 'LOGIN', generated: false }, { record: 'LOGIN.totp', envelope, generated: true }],
      establishingVmk: false,
    });
  });

  it.each([
    ['a required field left empty', { fields: [{ record: 'LOGIN', generated: false }, { record: 'LOGIN.totp', generated: false }], establishingVmk: false }],
    ['records out of order', { fields: [{ record: 'LOGIN.totp', envelope, generated: false }, { record: 'LOGIN', envelope, generated: false }], establishingVmk: false }],
    ['a missing field', { fields: [{ record: 'LOGIN', envelope, generated: false }], establishingVmk: false }],
    ['an envelope that is not one', { fields: [{ record: 'LOGIN', generated: false }, { record: 'LOGIN.totp', envelope: { ciphertext: 1 }, generated: false }], establishingVmk: false }],
    ['no establishing flag', { fields: [] }],
    ['not an object', null],
  ])('refuses %s', (_label, reply) => {
    expect(() => parseSecureWindowSeal(asked, reply)).toThrow();
  });
});
