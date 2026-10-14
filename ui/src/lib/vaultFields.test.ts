import { describe, expect, it } from 'vitest';
import { vaultHasNamedFieldShape } from './vaultFields';

describe('vaultHasNamedFieldShape', () => {
  it.each([
    [{ kind: 'static', default_field: 'value', fields: ['value'] }, false],
    [{ kind: 'static', default_field: 'password', fields: ['password'] }, true],
    [{ kind: 'static', default_field: 'password', fields: ['password', 'username'] }, true],
    [{ kind: 'static', default_field: 'value', fields: ['value'], fields_state: 'corrupt' }, true],
    [{ kind: 'keypair', default_field: 'value', fields: ['value'] }, false],
  ] as const)('classifies %o as %s', (secret, expected) => {
    expect(vaultHasNamedFieldShape(secret)).toBe(expected);
  });
});
