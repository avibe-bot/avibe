// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { VaultSandboxClient, authorizationWindowFeatures, openVaultAuthorizationWindow, resetVaultSandboxClient } from './vaultSandboxClient';
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

describe('VaultSandboxClient protected seed-phrase wallet (contract rev0.7 §4)', () => {
  const sealed = {
    envelope: { ciphertext: 'Y2lwaGVy', nonce: 'bm9uY2U=', wrap_meta: '{"v":1}' },
    accounts: Array.from({ length: 20 }, (_v, account) => ({
      account, evm: `02${'1'.repeat(64)}`, bitcoin: `03${'2'.repeat(64)}`, bitcoin_p2pkh: `02${'3'.repeat(64)}`, bitcoin_p2wpkh: `03${'4'.repeat(64)}`, solana: '5'.repeat(64),
    })),
    expectedAccount0: { eth: '0xAbC', btc_legacy: '1Abc', btc_segwit: 'bc1qabc', btc_taproot: 'bc1pabc', solana: 'Sol' },
    establishingVmk: false,
  };
  const client = (operations: string[], result?: unknown) => {
    const instance = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    instance.readyPromise = Promise.resolve({ type: 'ready', capabilities: { operations } });
    const request = vi.spyOn(instance, 'request').mockResolvedValue(result as never);
    return { instance: instance as unknown as VaultSandboxClient, request };
  };
  const withHd = ['status', 'setup', 'unlock', 'lock', 'seal', 'reveal', 'sign', 'approveRelease', 'sealHdWallet'];

  it('sends exactly {name, mode, wrapMeta}, the window id in the envelope, and returns the checked result', async () => {
    const { instance, request } = client(withHd, sealed);
    await expect(instance.sealHdWallet({ name: 'COLD', mode: 'import', wrapMeta: '{"v":1}' }, 'win1')).resolves.toEqual(sealed);
    expect(request).toHaveBeenCalledExactlyOnceWith('sealHdWallet', { name: 'COLD', mode: 'import', wrapMeta: '{"v":1}' }, {
      timeoutMs: 2_105_000, interactive: true, authorizationWindow: 'win1',
    });
  });

  it('is unavailable on a build without the operation, and tries nothing else', async () => {
    const { instance, request } = client(withHd.slice(0, -1));
    await expect(instance.sealHdWallet({ name: 'COLD', mode: 'create', wrapMeta: '{}' }, 'win1')).rejects.toMatchObject({ code: 'protected_hd_unavailable' });
    expect(request).not.toHaveBeenCalled();
  });

  it.each([
    ['the internal popup kind', { ...sealed, kind: 'sealed-hd-wallet' }],
    ['a short table', { ...sealed, accounts: sealed.accounts.slice(1) }],
    ['a missing setup flag', { envelope: sealed.envelope, accounts: sealed.accounts, expectedAccount0: sealed.expectedAccount0 }],
  ])('refuses %s', async (_label, result) => {
    const { instance } = client(withHd, result);
    await expect(instance.sealHdWallet({ name: 'COLD', mode: 'create', wrapMeta: '{}' }, 'win1')).rejects.toMatchObject({ code: 'protected_hd_result_invalid' });
  });
});

// A desktop shell that runs passkey steps itself must not trap the user there: when a step fails
// in the app for any reason other than the user cancelling, the page moves to the browser
// hand-off. The client is where every passkey-bearing step ends, so it is where that is decided;
// a new code the sandbox adds counts as a failure unless it is known to mean otherwise.
describe('VaultSandboxClient in a desktop shell that runs passkey steps', () => {
  type Shell = 'browser' | 'desktop' | 'in-app';
  const failure = (code: string) => ({ code });

  async function freshPage(shell: Shell) {
    // The hand-off is page-lifetime state; each case starts from a new page.
    vi.resetModules();
    const client = await import('./vaultSandboxClient');
    const handoff = await import('./vaultBrowserHandoff');
    if (shell !== 'browser') Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    if (shell === 'in-app') Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { configurable: true, value: true });
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);
    const instance = Object.create(client.VaultSandboxClient.prototype) as TestVaultSandboxClient & {
      pending: Map<string, TestPendingRequest & { reject: (error: Error) => void }>;
    };
    Object.assign(instance, {
      iframe, backdrop: null, pending: new Map(), readyPromise: Promise.resolve({}), handshaken: true,
      modalVisible: false, interactiveRequests: new Set(), surfaceRefreshTimer: null,
    });
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => undefined);
    // One sandbox step, answered as the sandbox would: `null` succeeds, a code fails.
    const step = async (
      op: string,
      outcome: { code: string } | null,
      options: { authorizationWindow?: string } = {},
      beforeAnswer?: () => void,
    ) => {
      const calls = postMessage.mock.calls.length;
      const result = instance.request(op, {}, options);
      await vi.waitFor(() => expect(postMessage.mock.calls.length).toBe(calls + 1));
      beforeAnswer?.();
      const pending = instance.pending.get((postMessage.mock.calls[calls][0] as { id: string }).id)!;
      window.clearTimeout(pending.timer);
      const error = outcome ? new client.VaultSandboxError(outcome.code, 'sandbox failure') : null;
      if (error) pending.reject(error);
      else pending.resolve({});
      await result.catch(() => undefined);
      return error;
    };
    // A sandbox window this page opened from the click, as approvals, signatures and reveals do.
    const reserveWindow = () => {
      stubReleaseFetch();
      const popup = stubBlankPopup();
      return { id: client.openVaultAuthorizationWindow()!.id, popup };
    };
    return { step, instance, postMessage, client, reserveWindow, handoff: handoff.vaultPasskeyHandoff, isFallback: handoff.isVaultPasskeyFallback };
  }

  afterEach(() => {
    Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
    Reflect.deleteProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__');
  });

  it.each([
    ['the window could not be opened', 'approveRelease', 'authorization_popup_blocked'],
    ['the setup window could not be opened', 'setup', 'setup_popup_blocked'],
    ['the passkey gave no PRF', 'unlock', 'passkey_prf_unavailable'],
    ['the ceremony errored', 'sign', 'sign_failed'],
    ['the window never answered', 'reveal', 'authorization_window_timeout'],
    ['a code this Avibe does not know', 'seal', 'some_future_failure'],
  ])('moves to the browser when %s', async (_label, op, code) => {
    const page = await freshPage('in-app');
    expect(page.handoff()).toBeNull();
    await page.step(op, failure(code));
    expect(page.handoff()).toBe('fallback');
  });

  it.each([
    ['the user closed the sandbox window', 'authorization_cancelled'],
    ['the user closed the window before confirming', 'authorization_window_closed'],
    ['the attempt was superseded', 'sandbox_reset'],
    ['the request expired, as it would in a browser', 'context_expired'],
    ['the request was invalid, as it would be in a browser', 'invalid_context'],
    ['this Avibe and the sandbox disagree, as they would in a browser', 'sandbox_integrity_failed'],
    ['the device is offline, as it would be in a browser', 'sandbox_offline'],
    ['the vault has no passkey, as in a browser', 'passkey_not_configured'],
  ])('stays in the app when %s', async (_label, code) => {
    const page = await freshPage('in-app');
    await page.step('approveRelease', failure(code));
    expect(page.handoff()).toBeNull();
  });

  // A system refusal before any passkey sheet reaches the sandbox as a cancel.
  it('respects one passkey cancel and offers the browser after a second in a row', async () => {
    const page = await freshPage('in-app');
    await page.step('unlock', failure('passkey_cancelled'));
    expect(page.handoff()).toBeNull();
    await page.step('unlock', failure('passkey_cancelled'));
    expect(page.handoff()).toBe('fallback');
  });

  // The protected seed-phrase window reports a refused passkey as its own cancel, so there that
  // cancel follows the same rule; elsewhere closing the window is only ever the user's choice.
  it('counts the seed-phrase window cancel like a passkey cancel', async () => {
    const page = await freshPage('in-app');
    await page.step('sealHdWallet', failure('authorization_cancelled'));
    expect(page.handoff()).toBeNull();
    await page.step('sealHdWallet', failure('authorization_cancelled'));
    expect(page.handoff()).toBe('fallback');
  });

  // A window this page reserved that the sandbox never loads into ends as a closed window.
  it('counts a reserved window that closed like a passkey cancel', async () => {
    const page = await freshPage('in-app');
    await page.step('approveRelease', failure('authorization_window_closed'), { authorizationWindow: page.reserveWindow().id });
    expect(page.handoff()).toBeNull();
    await page.step('reveal', failure('authorization_window_closed'), { authorizationWindow: page.reserveWindow().id });
    expect(page.handoff()).toBe('fallback');
  });

  it('never counts cancelling in an approval window as a refusal', async () => {
    const page = await freshPage('in-app');
    await page.step('approveRelease', failure('authorization_cancelled'), { authorizationWindow: page.reserveWindow().id });
    await page.step('approveRelease', failure('authorization_cancelled'), { authorizationWindow: page.reserveWindow().id });
    expect(page.handoff()).toBeNull();
  });

  // Without a window this page reserved, a closed window is the sandbox's own: only the user closes it.
  it('never counts closing a window the sandbox opened itself', async () => {
    const page = await freshPage('in-app');
    await page.step('approveRelease', failure('authorization_window_closed'));
    await page.step('approveRelease', failure('authorization_window_closed'));
    expect(page.handoff()).toBeNull();
  });

  it('starts counting cancels again after a passkey step succeeds', async () => {
    const page = await freshPage('in-app');
    await page.step('unlock', failure('passkey_cancelled'));
    await page.step('unlock', null);
    await page.step('reveal', failure('passkey_cancelled'));
    expect(page.handoff()).toBeNull();
  });

  it('ignores steps that never ask for the passkey', async () => {
    const page = await freshPage('in-app');
    await page.step('status', failure('sandbox_error'));
    expect(page.handoff()).toBeNull();
  });

  it.each([
    ['a desktop shell without the capability', 'desktop' as const, 'desktop'],
    ['a browser', 'browser' as const, null],
  ])('changes nothing in %s', async (_label, shell, expected) => {
    const page = await freshPage(shell);
    expect(page.handoff()).toBe(expected);
    await page.step('approveRelease', failure('authorization_popup_blocked'));
    expect(page.handoff()).toBe(expected);
  });

  // Only the attempt that moved the page reports it; its surface then shows the hand-off note.
  it('names the failure that moved the page, and refuses later steps without asking the sandbox', async () => {
    const page = await freshPage('in-app');
    const moved = await page.step('approveRelease', failure('authorization_popup_blocked'));
    expect(page.isFallback(moved)).toBe(true);
    expect(page.isFallback(new Error('grant refused'))).toBe(false);
    const posted = page.postMessage.mock.calls.length;
    const refused = page.instance.request('unlock', {}, {});
    await expect(refused).rejects.toMatchObject({ code: 'sandbox_browser_required' });
    expect(page.isFallback(await refused.catch((error: unknown) => error))).toBe(true);
    expect(page.postMessage.mock.calls.length).toBe(posted);
  });

  it('moves to the browser when the app does not open the sandbox window', async () => {
    const page = await freshPage('in-app');
    stubReleaseFetch();
    vi.spyOn(window, 'open').mockReturnValue(null);
    expect(page.client.openVaultAuthorizationWindow()).toBeNull();
    expect(page.handoff()).toBe('fallback');
  });

  // A reserved window that already reads closed was closed by the user, or declined by the app
  // while it was not in front (A2's contract). Trying again works, so it is only ever a cancel.
  it('never counts a reserved window that reads closed', async () => {
    const page = await freshPage('in-app');
    for (const op of ['approveRelease', 'sign']) {
      const reserved = page.reserveWindow();
      await page.step(op, failure('authorization_window_closed'), { authorizationWindow: reserved.id }, () => { reserved.popup.closed = true; });
    }
    expect(page.handoff()).toBeNull();
  });
});

// An approval or turn-off whose reserved window closed (the user closed it, or the app declined to
// show it) ends a few seconds later instead of after the frame's 30-second pairing timeout. Neither
// shows anything or waits on the user once its window has confirmed. A reveal does (the value is on
// screen until the user is done), and a signature's context is the request's own, so neither of
// those ever ends early.
describe('VaultSandboxClient request whose reserved window closed', () => {
  async function pending(op: string) {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval'] });
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);
    const instance = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    Object.assign(instance, {
      iframe, backdrop: null, pending: new Map(), readyPromise: Promise.resolve({}), handshaken: true,
      modalVisible: false, interactiveRequests: new Set(), surfaceRefreshTimer: null,
    });
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => undefined);
    stubReleaseFetch();
    const popup = stubBlankPopup();
    const authorization = openVaultAuthorizationWindow()!;
    const settled = vi.fn();
    // The interactive deadline the client gives these steps.
    const result = instance.request(op, {}, { interactive: true, authorizationWindow: authorization.id, timeoutMs: 5 * 60_000 });
    result.then((value) => settled({ value }), (error: unknown) => settled({ error }));
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledTimes(1));
    const id = (postMessage.mock.calls[0][0] as { id: string }).id;
    vi.spyOn(instance, 'startSurfaceRefresh').mockImplementation(() => undefined);
    // The sandbox's own events, as it posts them to this page.
    const sandboxEvent = (event: 'ui.show' | 'ui.hide') =>
      (instance as unknown as { handleEvent: (message: { event: string }) => void }).handleEvent({ event });
    return { instance, popup, settled, sandboxEvent, answer: (value: unknown) => instance.pending.get(id)?.resolve(value) };
  }

  afterEach(() => {
    vi.useRealTimers();
  });

  it('ends an approval soon after its window closed, and drops an answer that comes later', async () => {
    const step = await pending('approveRelease');
    step.popup.closed = true;
    await vi.advanceTimersByTimeAsync(2_000);
    expect(step.settled).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(step.settled).toHaveBeenCalledWith({ error: expect.objectContaining({ code: 'authorization_window_closed' }) });
    expect(step.instance.pending.size).toBe(0);
    step.answer({ blindBoxes: [{}] });
    await vi.advanceTimersByTimeAsync(0);
    expect(step.settled).toHaveBeenCalledTimes(1);
  });

  // The sandbox window closes itself after a confirmed approval, just before its answer arrives.
  it('keeps the answer of an approval whose window closed itself', async () => {
    const step = await pending('approveRelease');
    step.popup.closed = true;
    await vi.advanceTimersByTimeAsync(1_000);
    step.answer({ blindBoxes: [{}] });
    await vi.advanceTimersByTimeAsync(5_000);
    expect(step.settled).toHaveBeenCalledTimes(1);
    expect(step.settled).toHaveBeenCalledWith({ value: { blindBoxes: [{}] } });
  });

  // A frame that asks for its slot after the window closed (an approval that must confirm again
  // after the vault locked mid-step) is waiting on the user, so the step is not over.
  it('never ends an approval while the frame shows something', async () => {
    const step = await pending('approveRelease');
    step.popup.closed = true;
    step.sandboxEvent('ui.show');
    await vi.advanceTimersByTimeAsync(10_000);
    expect(step.settled).not.toHaveBeenCalled();
    expect(step.instance.modalVisible).toBe(true);
  });

  // The sandbox's own order for a reveal it shows in this page's frame: the window confirms and
  // closes itself, the frame shows the value, and the answer comes after the user's Done.
  it('never ends a reveal whose value is on screen in the frame', async () => {
    const step = await pending('reveal');
    step.popup.closed = true;
    step.sandboxEvent('ui.show');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(step.settled).not.toHaveBeenCalled();
    expect(step.instance.modalVisible).toBe(true);
    step.sandboxEvent('ui.hide');
    step.answer({ completed: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(step.settled).toHaveBeenCalledWith({ value: { completed: true } });
  });

  // A sandbox that shows the value in the secure window itself keeps that window open while it is
  // shown and answers after the user closes it, with nothing in the frame.
  it('never ends a reveal whose value is on screen in its window, nor after the user closed it', async () => {
    const step = await pending('reveal');
    await vi.advanceTimersByTimeAsync(60_000);
    step.popup.closed = true;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(step.settled).not.toHaveBeenCalled();
    step.answer({ completed: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(step.settled).toHaveBeenCalledWith({ value: { completed: true } });
  });

  it('never ends a signature early', async () => {
    const step = await pending('sign');
    step.popup.closed = true;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(step.settled).not.toHaveBeenCalled();
    step.answer({ signature: 'sig' });
    await vi.advanceTimersByTimeAsync(0);
    expect(step.settled).toHaveBeenCalledWith({ value: { signature: 'sig' } });
  });
});

describe('the secure window opens at a fixed size, centered over the Avibe window', () => {
  it('computes the same size every time and centers it on the opener', () => {
    vi.stubGlobal('screenX', 100);
    vi.stubGlobal('screenY', 50);
    vi.stubGlobal('outerWidth', 1440);
    vi.stubGlobal('outerHeight', 900);
    expect(authorizationWindowFeatures()).toBe('popup,width=520,height=760,left=560,top=120');
    // Never off the left or top edge, even from a small window.
    vi.stubGlobal('screenX', 0);
    vi.stubGlobal('screenY', 0);
    vi.stubGlobal('outerWidth', 400);
    vi.stubGlobal('outerHeight', 500);
    expect(authorizationWindowFeatures()).toBe('popup,width=520,height=760,left=0,top=0');
    // A display left of the primary one has negative coordinates: the window stays on it.
    vi.stubGlobal('screen', { availLeft: -1440, availTop: 0, availWidth: 1440, availHeight: 900 });
    vi.stubGlobal('screenX', -1440);
    vi.stubGlobal('screenY', 0);
    vi.stubGlobal('outerWidth', 1440);
    vi.stubGlobal('outerHeight', 900);
    expect(authorizationWindowFeatures()).toBe('popup,width=520,height=760,left=-980,top=70');
    // Kept within that screen, not pushed off its right edge.
    vi.stubGlobal('screenX', -400);
    expect(authorizationWindowFeatures()).toBe('popup,width=520,height=760,left=-520,top=70');
    vi.unstubAllGlobals();
  });
});
