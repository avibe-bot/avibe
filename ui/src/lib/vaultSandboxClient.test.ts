// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { VaultSandboxClient, openVaultAuthorizationWindow, resetVaultSandboxClient } from './vaultSandboxClient';
import { stubBlankPopup, stubReleaseFetch } from './__fixtures__/vaultSandboxTransport';
import type { VaultConfirmSurface } from './vaultConfirmSurface';
import type { PolicyDowngradeContext } from './vaultSigningPolicy';
import vectors from './__fixtures__/policy-downgrade-wire-vectors.json';

const visibleSurface: VaultConfirmSurface = {
  frame: {
    frameWidth: 440,
    frameHeight: 640,
    intersectionRatio: 1,
    visibleByIntersectionObserver: true,
    opacity: 1,
    pointerEvents: true,
  },
  sampledAt: 1_700_000_000_000,
};

type TestPendingRequest = {
  timer: number;
  resolve: (value: unknown) => void;
};

type TestVaultSandboxClient = {
  iframe: HTMLIFrameElement;
  backdrop: HTMLDivElement | null;
  pending: Map<string, TestPendingRequest>;
  readyPromise: Promise<unknown>;
  handshaken: boolean;
  modalVisible: boolean;
  interactiveRequests: Set<string>;
  surfaceRefreshTimer: number | null;
  setModalVisible: (visible: boolean) => void;
  startSurfaceRefresh: () => void;
  measureSurface: () => Promise<VaultConfirmSurface | null>;
  request: <T>(
    op: string,
    payload: unknown,
    options: { timeoutMs?: number; interactive?: boolean; authorizationWindow?: string },
  ) => Promise<T>;
};

afterEach(() => {
  resetVaultSandboxClient();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(document, 'elementFromPoint');
  document.body.replaceChildren();
});

describe('VaultSandboxClient interactive request surface', () => {
  it('attests the expanded frame geometry and observer visibility', async () => {
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);
    vi.spyOn(iframe, 'getBoundingClientRect').mockReturnValue({
      x: 100,
      y: 50,
      left: 100,
      top: 50,
      right: 540,
      bottom: 690,
      width: 440,
      height: 640,
      toJSON: () => ({}),
    } as DOMRect);
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(private readonly callback: IntersectionObserverCallback) {}
        observe(): void {
          this.callback(
            [{ intersectionRatio: 1, isVisible: false } as unknown as IntersectionObserverEntry],
            this as unknown as IntersectionObserver,
          );
        }
        disconnect(): void {}
      },
    );

    const client = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    Object.assign(client, { iframe });
    const surface = await client.measureSurface();

    expect(surface?.frame).toMatchObject({
      frameWidth: 440,
      frameHeight: 640,
      intersectionRatio: 1,
      visibleByIntersectionObserver: false,
    });
  });

  it('expands the iframe before measuring and sending an interactive RPC', async () => {
    const iframe = document.createElement('iframe');
    iframe.style.width = '0';
    iframe.style.height = '0';
    iframe.style.visibility = 'hidden';
    iframe.style.pointerEvents = 'none';
    document.body.appendChild(iframe);

    const client = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    Object.assign(client, {
      iframe,
      backdrop: null,
      pending: new Map(),
      readyPromise: Promise.resolve({}),
      handshaken: true,
      modalVisible: false,
      interactiveRequests: new Set(),
      surfaceRefreshTimer: null,
    });
    vi.spyOn(client, 'startSurfaceRefresh').mockImplementation(() => undefined);
    const setModalVisible = vi.spyOn(client, 'setModalVisible');
    const measure = vi.spyOn(client, 'measureSurface').mockImplementation(async () => {
      expect(setModalVisible).toHaveBeenCalledWith(true);
      expect(client['modalVisible']).toBe(true);
      expect(iframe.style.visibility).toBe('visible');
      expect(iframe.style.pointerEvents).toBe('auto');
      return visibleSurface;
    });
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => undefined);

    const result = client.request<{ blindBoxes: unknown[] }>(
      'approveRelease',
      { items: [] },
      { interactive: true },
    );
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledTimes(1));

    const [wire] = postMessage.mock.calls[0];
    expect(measure).toHaveBeenCalledOnce();
    expect(wire).toMatchObject({ op: 'approveRelease', surface: visibleSurface });
    const pending = client.pending.get((wire as { id: string }).id);
    expect(pending).toBeDefined();
    if (!pending) throw new Error('interactive request was not registered');
    window.clearTimeout(pending.timer);
    pending.resolve({ blindBoxes: [] });
    await expect(result).resolves.toEqual({ blindBoxes: [] });
  });

  // The approval click already opened the sandbox authorization window, so the sandbox skips its
  // launcher card. Expanding the frame here would bring back the duplicate review surface this
  // path removes, and dropping the window id would make the sandbox show that card again.
  it('keeps the frame collapsed and names the authorization window the parent opened', async () => {
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);

    const client = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    Object.assign(client, {
      iframe,
      backdrop: null,
      pending: new Map(),
      readyPromise: Promise.resolve({}),
      handshaken: true,
      modalVisible: false,
      interactiveRequests: new Set(),
      surfaceRefreshTimer: null,
    });
    const setModalVisible = vi.spyOn(client, 'setModalVisible');
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => undefined);

    stubReleaseFetch();
    stubBlankPopup();
    const authorization = openVaultAuthorizationWindow()!;
    const result = client.request<{ blindBoxes: unknown[] }>(
      'approveRelease',
      { items: [] },
      { interactive: true, authorizationWindow: authorization.id },
    );
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledTimes(1));

    const [wire] = postMessage.mock.calls[0];
    expect(wire).toMatchObject({ op: 'approveRelease', authorizationWindow: authorization.id });
    expect(wire).not.toHaveProperty('surface');
    expect(setModalVisible).not.toHaveBeenCalled();
    expect(iframe.style.visibility).not.toBe('visible');
    // A fallback launcher still gets a surface attestation once the sandbox asks for its slot.
    const requestId = (wire as { id: string }).id;
    expect(client.interactiveRequests.has(requestId)).toBe(true);
    const pending = client.pending.get(requestId);
    if (!pending) throw new Error('interactive request was not registered');
    window.clearTimeout(pending.timer);
    pending.resolve({ blindBoxes: [] });
    await expect(result).resolves.toEqual({ blindBoxes: [] });
  });

  it('collapses the modal when interactive surface measurement fails before send', async () => {
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);

    const client = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    Object.assign(client, {
      iframe,
      backdrop: null,
      pending: new Map(),
      readyPromise: Promise.resolve({}),
      handshaken: true,
      modalVisible: false,
      interactiveRequests: new Set(),
      surfaceRefreshTimer: null,
    });
    vi.spyOn(client, 'startSurfaceRefresh').mockImplementation(() => undefined);
    vi.spyOn(client, 'measureSurface').mockRejectedValue(new Error('visibility API failed'));
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => undefined);

    await expect(
      client.request('approveRelease', { items: [] }, { interactive: true }),
    ).rejects.toThrow('visibility API failed');

    expect(postMessage).not.toHaveBeenCalled();
    expect(client.interactiveRequests.size).toBe(0);
    expect(client.pending.size).toBe(0);
    expect(client.modalVisible).toBe(false);
    expect(client.backdrop).toBeNull();
    expect(iframe.style.visibility).toBe('hidden');
    expect(iframe.style.pointerEvents).toBe('none');
  });
});

// A sandbox release that cannot create Ed25519 keys ignores the curve and seals a secp256k1 key.
// Saving that as a Solana key would pin a key that cannot sign what it claims, so the client
// accepts only an Ed25519 record naming the key it reported, and asks for one only when told to.
describe('VaultSandboxClient Solana keypair seal', () => {
  const ed25519Key = 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a';
  const sealed = (recordMeta: Record<string, unknown>, publicKey: string) => ({
    envelope: { ciphertext: 'c', nonce: 'n', wrap_meta: JSON.stringify({ v: 1, scheme: 'machine-aesgcm-v1', record_meta: recordMeta }) },
    establishingVmk: false,
    publicKey,
    addresses: { solana: 'FVen3X669xLzsi6N2V91DoiyzHzg1uAgqiT8jZ9nS96Z' },
  });
  const client = (result: unknown) => {
    const instance = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    const request = vi.spyOn(instance, 'request').mockResolvedValue(result as never);
    return { instance: instance as unknown as VaultSandboxClient, request };
  };

  it('asks for an Ed25519 key and returns the record it names', async () => {
    const result = sealed({ kind: 'keypair', curve: 'ed25519', public_key: ed25519Key }, ed25519Key);
    const { instance, request } = client(result);
    await expect(instance.seal({ name: 'SOL_KEY', kind: 'keypair', curve: 'ed25519', wrapMeta: 'vmk' })).resolves.toEqual(result);
    expect(request.mock.calls[0][0]).toBe('seal');
    expect(request.mock.calls[0][1]).toEqual({ name: 'SOL_KEY', kind: 'keypair', curve: 'ed25519', wrapMeta: 'vmk' });
  });

  it('sends a secp256k1 keypair request without a curve, as before', async () => {
    const { instance, request } = client(sealed({ kind: 'keypair', public_key: `02${'11'.repeat(32)}` }, `02${'11'.repeat(32)}`));
    await instance.seal({ name: 'ETH_KEY', kind: 'keypair', wrapMeta: 'vmk' });
    expect(request.mock.calls[0][1]).toEqual({ name: 'ETH_KEY', kind: 'keypair', wrapMeta: 'vmk' });
  });

  it.each([
    ['a secp256k1 record', { kind: 'keypair', public_key: `02${'11'.repeat(32)}` }, `02${'11'.repeat(32)}`],
    ['an Ed25519 record for another key', { kind: 'keypair', curve: 'ed25519', public_key: '22'.repeat(32) }, ed25519Key],
    ['an Ed25519 record with an uppercase key', { kind: 'keypair', curve: 'ed25519', public_key: ed25519Key.toUpperCase() }, ed25519Key.toUpperCase()],
  ])('refuses %s when an Ed25519 key was asked for', async (_label, recordMeta, publicKey) => {
    const { instance } = client(sealed(recordMeta, publicKey));
    await expect(instance.seal({ name: 'SOL_KEY', kind: 'keypair', curve: 'ed25519' })).rejects.toMatchObject({ code: 'sandbox_curve_unsupported' });
  });
});

describe('VaultSandboxClient signing-policy downgrade (contract v1.1 §B)', () => {
  const context = vectors.vectors[0].context as unknown as PolicyDowngradeContext;
  const assertion = {
    id: 'AQID',
    rawId: 'AQID',
    type: 'public-key',
    response: { clientDataJSON: 'e30=', authenticatorData: 'AAAA', signature: 'MEUCIQ==', userHandle: null },
  };
  const client = (operations: string[], result?: unknown) => {
    const instance = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    instance.readyPromise = Promise.resolve({ type: 'ready', capabilities: { operations } });
    const request = vi.spyOn(instance, 'request').mockResolvedValue(result as never);
    return { instance: instance as unknown as VaultSandboxClient, request };
  };
  const withPolicy = ['status', 'setup', 'unlock', 'lock', 'seal', 'reveal', 'sign', 'approveRelease', 'authorizeSigningPolicyDowngrade'];

  it('sends exactly {context} on an interactive request with the parent-opened window and returns the checked result', async () => {
    const { instance, request } = client(withPolicy, { challengeId: context.challengeId, assertion });
    await expect(instance.authorizeSigningPolicyDowngrade({ context }, 'win1')).resolves.toEqual({ challengeId: context.challengeId, assertion });
    expect(request).toHaveBeenCalledExactlyOnceWith('authorizeSigningPolicyDowngrade', { context }, expect.objectContaining({
      interactive: true, authorizationWindow: 'win1', timeoutMs: 5 * 60_000,
    }));
    expect(Object.keys(request.mock.calls[0][1] as object)).toEqual(['context']);
  });

  it('is unavailable on a build without the operation, and tries no other RPC', async () => {
    const { instance, request } = client(withPolicy.slice(0, -1));
    await expect(instance.supports('authorizeSigningPolicyDowngrade')).resolves.toBe(false);
    await expect(instance.authorizeSigningPolicyDowngrade({ context })).rejects.toMatchObject({ code: 'policy_downgrade_unavailable' });
    expect(request).not.toHaveBeenCalled();
  });

  it.each([
    ['a boolean', true],
    ['another challenge', { challengeId: 'vop_other', assertion }],
    ['an extra field', { challengeId: context.challengeId, assertion, ok: true }],
    ['no assertion', { challengeId: context.challengeId }],
    ['an assertion with extra fields', { challengeId: context.challengeId, assertion: { ...assertion, clientExtensionResults: {} } }],
    ['a non public-key credential', { challengeId: context.challengeId, assertion: { ...assertion, type: 'password' } }],
    ['a URL-safe rawId', { challengeId: context.challengeId, assertion: { ...assertion, rawId: 'AQ_-' } }],
    ['a missing signature', { challengeId: context.challengeId, assertion: { ...assertion, response: { ...assertion.response, signature: '' } } }],
    ['an extra response field', { challengeId: context.challengeId, assertion: { ...assertion, response: { ...assertion.response, prf: 'x' } } }],
  ])('refuses %s as the sandbox result', async (_label, result) => {
    const { instance } = client(withPolicy, result);
    await expect(instance.authorizeSigningPolicyDowngrade({ context })).rejects.toMatchObject({ code: 'policy_result_invalid' });
  });

  it('keeps an older pinned build usable for its own operations (the op is not globally required)', async () => {
    const { instance, request } = client(withPolicy.slice(0, -1), { completed: true });
    await expect(instance.reveal({ material: { name: 'A', envelope: {} as never }, context: {} as never })).resolves.toEqual({ completed: true });
    expect(request.mock.calls[0][0]).toBe('reveal');
  });
});
