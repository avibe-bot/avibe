import { describe, expect, it } from 'vitest';
import type { VaultSecret } from '../context/ApiContext';
import { matchesVaultQuery, vaultSearchTerms } from './vaultSearch';

// The list endpoint carries public metadata only. A future response extension
// must not silently make values or unrelated policy properties searchable.
const secret: VaultSecret = {
  name: 'STRIPE_TOKEN', description: '支付 release automation',
  tags: ['production', 'skill:billing-release'], kind: 'static', protection: 'protected',
  signer_kind: null, source: 'user',
  policy: { allowed_hosts: ['api.stripe.com'], auth: { type: 'header', name: 'X-API-Key' } },
  signing_addresses: { eth: '0xABC123' },
  last_used_at: null, use_count: 0, created_at: '', updated_at: '',
};

describe('Vault metadata search (literal, case-insensitive AND)', () => {
  it.each([
    ['', true], ['  \t\n', true], ['StRiPe', true],
    ['\tSTRIPE\nPRODUCTION  ', true], ['stripe missing', false],
    ['支付 automation', true], ['billing-release api.stripe.com', true],
    ['header x-api-key', true], ['protected static', true],
    ['保护档 静态值', true], ['0xabc123', true],
    ['standard', false], ['skill:', false], ['.*', false],
  ])('query %j matches = %s', (query, expected) => {
    expect(matchesVaultQuery(secret, vaultSearchTerms(query), ['保护档', '静态值'])).toBe(expected);
  });

  it('searches raw and localized signing metadata', () => {
    const keypair = { ...secret, kind: 'keypair', signer_kind: 'secp256k1', protection: 'standard' };
    expect(matchesVaultQuery(keypair, vaultSearchTerms('keypair secp256k1 签名密钥 标准档'), ['签名密钥', '标准档'])).toBe(true);
  });

  it.each(['hidden-value', 'auth-password', 'private-material', 'sealed-bytes', 'policy-value'])('never searches %s', (query) => {
    const extended = {
      ...secret, value: 'hidden-value', private_key: 'private-material', ciphertext: 'sealed-bytes',
      policy: { ...secret.policy, value: 'policy-value', auth: { type: 'header', name: 'X-API-Key', value: 'auth-password' } },
    };
    expect(matchesVaultQuery(extended, vaultSearchTerms(query), [])).toBe(false);
  });
});
