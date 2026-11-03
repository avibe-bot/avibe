// @vitest-environment jsdom

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useProtectedVault } from './useProtectedVault';
import { VaultSandboxClient, type VaultSandboxSigningContext, type VaultSignedOperationContext } from './vaultSandboxClient';

const fixture = JSON.parse(readFileSync(resolve(process.cwd(), '../tests/fixtures/vault_signing/protected-sign-context.json'), 'utf8')) as {
  signedContext: VaultSignedOperationContext;
};
const bitcoinVectors = JSON.parse(readFileSync(resolve(process.cwd(), '../tests/fixtures/vault_bitcoin/core-fixtures.json'), 'utf8'));
const sandbox = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => ({}) }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: sandbox.get,
}));

afterEach(() => {
  cleanup();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('protected signing metadata → hook → real client → RPC', () => {
  // Card-only mocks missed material.kind being forwarded to the strict sandbox parser.
  // Only handshake/status and the transport response are simulated here; neither the hook's
  // sign method nor the client's serialization is replaced.
  it.each(['raw', 'bitcoin'] as const)('sends exactly the %s sandbox sign contract, not approval-card metadata', async (kind) => {
    vi.stubGlobal('BroadcastChannel', undefined);
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);
    const pending = new Map<string, { timer: number; resolve: (value: unknown) => void }>();
    const client = Object.create(VaultSandboxClient.prototype) as VaultSandboxClient;
    Object.assign(client, {
      iframe, backdrop: null, pending, readyPromise: Promise.resolve({}), handshaken: true,
      modalVisible: false, interactiveRequests: new Set(), surfaceRefreshTimer: null,
    });
    vi.spyOn(client, 'status').mockResolvedValue({ state: 'locked' });
    sandbox.get.mockResolvedValue(client);
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => undefined);
    const { result } = renderHook(() => useProtectedVault());
    const context = structuredClone(fixture.signedContext) as VaultSignedOperationContext;
    if (kind === 'bitcoin') {
      const vector = bitcoinVectors.cases[0];
      context.release!.scheme = 'schnorr-secp256k1-bip341-keypath';
      context.release!.digest = vector.expected.taproot_digest_hex;
      context.release!.signingContext = {
        kind: 'bitcoin-taproot-keypath-v1', network: 'signet', unsignedTransaction: vector.unsigned_tx_hex,
        prevout: { amountSats: vector.funding.spent_output_value_sats, scriptPubKey: vector.funding.spent_output_script_hex },
        digestAlgorithm: 'bip341-sighash-default-keypath', digest: vector.expected.taproot_digest_hex,
      };
    }
    // This boundary checks transport and independent parsing, not the fixture's daemon signature.
    // Actual daemon signatures and fresh-UV use are tested by service and main.ts RPC tests.
    const scheme = context.release!.scheme;
    const signingContext = context.release!.signingContext as VaultSandboxSigningContext;
    const envelope = { ciphertext: 'test-envelope', nonce: 'test-nonce', wrap_meta: 'test-meta' };
    // This is the server's hydrated keypair material shape, not a hand-projected RPC object.
    const material = { name: 'PRIVATE_TEST_KEY', kind: 'keypair', envelope };
    const signature = { signature: '01'.repeat(64) };
    let signing!: Promise<unknown>;
    await act(async () => {
      signing = result.current.signProtectedRequest(material, signingContext, scheme, context, 'window-sign-test');
    });
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledOnce());
    const [wire] = postMessage.mock.calls[0] as unknown as [{ id: string; op: string; payload: unknown; authorizationWindow: string }];
    expect(wire.op).toBe('sign');
    expect(wire.authorizationWindow).toBe('window-sign-test');
    expect(wire.payload).toEqual({
      material: { name: material.name, envelope },
      scheme, signingContext, context,
    });
    expect(material.kind).toBe('keypair'); // Projection must not mutate card data used elsewhere.
    // Optional cross-repository acceptance uses the real consumer, bundled from a specified
    // sandbox checkout. No parser copy or permissive mock can hide a wire-shape disagreement.
    if (process.env.AVIBE_TEST_SANDBOX_SIGN_PARSER) {
      execFileSync(process.execPath, ['--input-type=module', '-e', `
        import assert from 'node:assert/strict';
        import { readFileSync } from 'node:fs';
        import { pathToFileURL } from 'node:url';
        const { parseSignRequest, verifySigningContext } = await import(pathToFileURL(process.argv[1]).href);
        const payload = JSON.parse(readFileSync(0, 'utf8'));
        assert.deepEqual(parseSignRequest(payload), payload);
        const verified = verifySigningContext(payload.signingContext);
        assert.equal(verified.digest, payload.context.release.digest);
        if (payload.signingContext.kind === 'bitcoin-taproot-keypath-v1') {
          assert.match(verified.display, /not chain proof/);
          assert.throws(() => verifySigningContext({ ...payload.signingContext, prevout: { ...payload.signingContext.prevout, amountSats: payload.signingContext.prevout.amountSats + 1 } }), /digest/);
        }
        assert.throws(() => parseSignRequest({ ...payload, material: { ...payload.material, kind: 'keypair' } }), /unsupported fields/);
      `, process.env.AVIBE_TEST_SANDBOX_SIGN_PARSER], { input: JSON.stringify(wire.payload), encoding: 'utf8' });
    }
    const request = pending.get(wire.id);
    if (!request) throw new Error('sign request was not registered');
    window.clearTimeout(request.timer);
    await act(async () => { request.resolve(signature); await expect(signing).resolves.toEqual(signature); });
  });
});
