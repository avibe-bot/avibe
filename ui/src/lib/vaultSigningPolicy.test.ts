import { describe, expect, it, vi } from 'vitest';

import vectors from './__fixtures__/policy-downgrade-wire-vectors.json';
import { factorForAssertion, turnOffAskBeforeSigning, type PolicyDowngradeContext } from './vaultSigningPolicy';

const context = vectors.vectors[0].context as unknown as PolicyDowngradeContext;
const credentialId = context.factors[0].credentialId;
const assertion = {
  id: 'UFVCTElDLUNSRURFTlRJQUwtSUQtTk9ULUEtUkVBTC1DUkVERU5USUFM',
  rawId: credentialId,
  type: 'public-key' as const,
  response: { clientDataJSON: 'e30=', authenticatorData: 'AAAA', signature: 'MEUCIQ==', userHandle: null },
};

const fakes = (over: { challenge?: unknown; sandbox?: () => Promise<unknown>; complete?: unknown } = {}) => {
  const api = {
    createVaultSigningPolicyChallenge: vi.fn(async () => (over.challenge ?? { ok: true, context }) as never),
    completeVaultSigningPolicyDowngrade: vi.fn(async () => (over.complete ?? { ok: true, secret: { name: 'K', policy: {} } }) as never),
  };
  const sandbox = { authorizeSigningPolicyDowngrade: vi.fn(over.sandbox ?? (async () => ({ challengeId: context.challengeId, assertion }))) };
  return { api, sandbox, getSandbox: async () => sandbox as never };
};

describe('factorForAssertion', () => {
  it('matches the credential by its bytes', () => {
    expect(factorForAssertion(context, credentialId)).toBe('vaf_public_wire_vector');
  });
  it.each([
    ['another credential', 'AQID'],
    ['URL-safe text for the same bytes', credentialId.replace(/\+/g, '-').replace(/\//g, '_') + '#'],
    ['empty', ''],
  ])('finds none for %s', (_label, rawId) => {
    expect(factorForAssertion(context, rawId)).toBeNull();
  });
  it('finds none when two factors share the bytes', () => {
    const twice = { ...context, factors: [context.factors[0], { factorId: 'vaf_b', credentialId }] };
    expect(factorForAssertion(twice, credentialId)).toBeNull();
  });
});

describe('turnOffAskBeforeSigning', () => {
  it('asks for the account’s challenge, has the sandbox authorize that context, and commits the matched factor', async () => {
    const { api, sandbox, getSandbox } = fakes();
    const outcome = await turnOffAskBeforeSigning(api, getSandbox, { name: 'WALLET', account: 0 }, 'win');
    expect(outcome).toEqual({ ok: true, secret: { name: 'K', policy: {} } });
    expect(api.createVaultSigningPolicyChallenge).toHaveBeenCalledExactlyOnceWith('WALLET', { always_ask: false, account: 0 });
    expect(sandbox.authorizeSigningPolicyDowngrade).toHaveBeenCalledExactlyOnceWith({ context }, 'win');
    expect(api.completeVaultSigningPolicyDowngrade).toHaveBeenCalledExactlyOnceWith('WALLET', {
      context,
      authorization: { kind: 'webauthn', challenge_id: context.challengeId, factor_id: 'vaf_public_wire_vector', assertion },
    });
  });

  it('asks nothing without its own authorization window (blocked pop-up), never the inline sandbox', async () => {
    const { api, getSandbox, sandbox } = fakes();
    await expect(turnOffAskBeforeSigning(api, getSandbox, { name: 'K' }, undefined)).resolves.toEqual({ ok: false, reason: 'popup_blocked' });
    expect(api.createVaultSigningPolicyChallenge).not.toHaveBeenCalled();
    expect(sandbox.authorizeSigningPolicyDowngrade).not.toHaveBeenCalled();
  });

  it('sends no account for an ordinary key', async () => {
    const { api, getSandbox } = fakes();
    await turnOffAskBeforeSigning(api, getSandbox, { name: 'ETH_KEY' }, 'win');
    expect(api.createVaultSigningPolicyChallenge).toHaveBeenCalledWith('ETH_KEY', { always_ask: false });
  });

  it.each([
    ['vault_policy_setup_required', 'setup_required'],
    ['vault_policy_legacy_registration_missing', 'legacy_registration'],
    ['vault_policy_factor_unavailable', 'factor_unavailable'],
    ['vault_policy_context_unsupported', 'unavailable'],
    ['vault_policy_state_changed', 'changed'],
    ['forbidden', 'failed'],
  ])('stops before the sandbox when the challenge is refused with %s', async (code, reason) => {
    const { api, sandbox, getSandbox } = fakes({ challenge: { ok: false, code } });
    await expect(turnOffAskBeforeSigning(api, getSandbox, { name: 'K' }, 'win')).resolves.toEqual({ ok: false, reason });
    expect(sandbox.authorizeSigningPolicyDowngrade).not.toHaveBeenCalled();
    expect(api.completeVaultSigningPolicyDowngrade).not.toHaveBeenCalled();
  });

  it.each([
    ['passkey_cancelled', 'cancelled'],
    ['authorization_cancelled', 'cancelled'],
    // The codes the pinned build (fc013b75) actually emits for a closed, blocked or silent window.
    ['authorization_window_closed', 'cancelled'],
    ['authorization_popup_blocked', 'popup_blocked'],
    ['authorization_window_timeout', 'expired'],
    // fc013 decides the window's visibility once as it opens (covered, off screen, screen locked).
    ['sandbox_not_visible', 'not_visible'],
    ['sandbox_reset', 'cancelled'],
    ['policy_downgrade_unavailable', 'unavailable'],
    ['policy_context_expired', 'expired'],
    ['policy_context_clock_skew', 'clock_skew'],
    ['policy_result_invalid', 'failed'],
  ])('commits nothing when the sandbox fails with %s', async (code, reason) => {
    const { api, getSandbox } = fakes({ sandbox: async () => { throw Object.assign(new Error(code), { code }); } });
    await expect(turnOffAskBeforeSigning(api, getSandbox, { name: 'K' }, 'win')).resolves.toEqual({ ok: false, reason });
    expect(api.completeVaultSigningPolicyDowngrade).not.toHaveBeenCalled();
  });

  it('commits nothing when the assertion matches no offered factor', async () => {
    const { api, getSandbox } = fakes({ sandbox: async () => ({ challengeId: context.challengeId, assertion: { ...assertion, rawId: 'AQID' } }) });
    await expect(turnOffAskBeforeSigning(api, getSandbox, { name: 'K' }, 'win')).resolves.toEqual({ ok: false, reason: 'failed' });
    expect(api.completeVaultSigningPolicyDowngrade).not.toHaveBeenCalled();
  });

  it.each([
    ['vault_policy_authz_expired', 'expired'],
    ['vault_policy_authz_used', 'failed'],
    ['vault_policy_authz_invalid', 'failed'],
    ['vault_policy_state_changed', 'changed'],
  ])('reports %s from the commit as not done', async (code, reason) => {
    const { api, getSandbox } = fakes({ complete: { ok: false, code } });
    await expect(turnOffAskBeforeSigning(api, getSandbox, { name: 'K' }, 'win')).resolves.toEqual({ ok: false, reason });
  });

  it('asks for a new challenge on every attempt (a retry never reuses an assertion)', async () => {
    const { api, sandbox, getSandbox } = fakes({ complete: { ok: false, code: 'vault_policy_authz_expired' } });
    await turnOffAskBeforeSigning(api, getSandbox, { name: 'K' }, 'win');
    await turnOffAskBeforeSigning(api, getSandbox, { name: 'K' }, 'win');
    expect(api.createVaultSigningPolicyChallenge).toHaveBeenCalledTimes(2);
    expect(sandbox.authorizeSigningPolicyDowngrade).toHaveBeenCalledTimes(2);
  });
});
