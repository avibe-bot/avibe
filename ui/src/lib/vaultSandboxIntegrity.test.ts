// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  getActiveVaultSandboxClient, getVaultSandboxClient, openVaultAuthorizationWindow,
  resetVaultSandboxClient, verifyVaultSandboxIntegrity,
} from './vaultSandboxClient';
import {
  VAULT_SANDBOX_EXPECTED_BUILD_HASH, VAULT_SANDBOX_IFRAME_URL, VAULT_SANDBOX_MANIFEST_PATH,
  VAULT_SANDBOX_ORIGIN, VAULT_SANDBOX_PINNED_MANIFEST, VAULT_SANDBOX_VERSION,
} from './vaultSandboxManifest';
import { releaseBytes, releaseResponse, sandboxMessage, stubBlankPopup, stubReleaseFetch } from './__fixtures__/vaultSandboxTransport';

vi.mock('./vaultSandboxPolicy', () => ({
  refreshVaultSandboxPolicy: vi.fn().mockResolvedValue(undefined),
  getVaultSandboxPolicy: () => ({ windowSeconds: 600, strictApprovals: true, parentValueSealAllowed: true }),
}));
const build = { buildHash: VAULT_SANDBOX_EXPECTED_BUILD_HASH, sandboxVersion: VAULT_SANDBOX_VERSION };
const accepted = { accepted: true, channel: 'avibe.vault.crypto', version: 2, sandboxOrigin: VAULT_SANDBOX_ORIGIN, build };
const ready = { channel: 'avibe.vault.crypto', version: 2, type: 'ready', build,
  capabilities: { operations: ['status', 'setup', 'unlock', 'lock', 'seal', 'reveal', 'sign', 'approveRelease'] } };

beforeEach(() => { stubReleaseFetch(); });
afterEach(() => {
  resetVaultSandboxClient();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function creation(handshake: unknown = accepted, readyMessage: unknown = ready) {
  const promise = getVaultSandboxClient();
  void promise.catch(() => undefined);
  await vi.waitFor(() => expect(document.querySelector('iframe')).not.toBeNull());
  const iframe = document.querySelector('iframe')!;
  const post = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation((message) => {
    if (message.op === 'handshake') queueMicrotask(() => sandboxMessage(iframe, {
      channel: 'avibe.vault.crypto', version: 2, id: message.id, ok: true, result: handshake,
    }));
  });
  sandboxMessage(iframe, readyMessage);
  return { promise, iframe, post };
}

describe('fail-closed local release authority', () => {
  it('hashes the exact manifest and every runtime resource with no successful cache', async () => {
    await verifyVaultSandboxIntegrity();
    const count = Object.keys(VAULT_SANDBOX_PINNED_MANIFEST.resources).length + 1;
    expect(fetch).toHaveBeenCalledTimes(count);
    for (const [url, options] of vi.mocked(fetch).mock.calls) {
      expect(String(url)).toMatch(/^https:\/\/sandbox\.avibe\.bot\/v\/[a-f0-9]{64}\//);
      expect(options).toMatchObject({ credentials: 'omit', cache: 'no-store', mode: 'cors', redirect: 'error' });
    }
    await verifyVaultSandboxIntegrity();
    expect(fetch).toHaveBeenCalledTimes(count * 2);
  });

  it.each(['missing', 'malformed', 'stale-manifest', 'tampered-html', 'tampered-js', 'tampered-css', 'redirect', 'origin', 'cors', 'network'])(
    'rejects %s before frame creation (the old disabled gate fails this regression)', async (failure) => {
      vi.mocked(fetch).mockImplementation(async (input) => {
        const url = String(input);
        if (failure === 'network') throw new TypeError('CORS or offline');
        if (failure === 'missing') return new Response('not found', { status: 404 });
        if (failure === 'redirect') {
          const response = releaseResponse(url);
          Object.defineProperty(response, 'redirected', { value: true });
          return response;
        }
        if (failure === 'origin' || failure === 'cors') {
          const response = new Response(releaseBytes(url) as Uint8Array<ArrayBuffer>);
          Object.defineProperties(response, { url: { value: failure === 'origin' ? 'https://other.test/asset' : url }, type: { value: failure === 'cors' ? 'opaque' : 'cors' } });
          return response;
        }
        const extension = failure.split('-')[1];
        if ((failure === 'malformed' || failure === 'stale-manifest') && url.endsWith('build-manifest.json')) {
          return releaseResponse(url, new TextEncoder().encode(failure === 'malformed' ? '{invalid' : JSON.stringify({ ...VAULT_SANDBOX_PINNED_MANIFEST, buildHash: 'dev' })));
        }
        const bytes = releaseBytes(url);
        if (failure.startsWith('tampered-') && url.endsWith(`.${extension}`)) bytes[0] ^= 1;
        return releaseResponse(url, bytes);
      });
      await expect(getVaultSandboxClient()).rejects.toMatchObject({ code: 'sandbox_integrity_failed' });
      expect(document.querySelector('iframe')).toBeNull();
      expect(getActiveVaultSandboxClient()).toBeNull();
    },
  );

  it('refuses missing Web Crypto and does not fetch or create a frame', async () => {
    vi.stubGlobal('crypto', { getRandomValues: vi.fn() });
    await expect(getVaultSandboxClient()).rejects.toMatchObject({ code: 'sandbox_integrity_failed' });
    expect(fetch).not.toHaveBeenCalled();
    expect(document.querySelector('iframe')).toBeNull();
  });

  it('accepts only matching ready and handshake identity, then exposes the client', async () => {
    const { promise, iframe, post } = await creation();
    const client = await promise;
    expect(iframe.src).toBe(VAULT_SANDBOX_IFRAME_URL);
    expect(getActiveVaultSandboxClient()).toBe(client);
    expect(post.mock.calls[0][0]).toMatchObject({ op: 'handshake', payload: { expectedBuildHash: build.buildHash } });
  });

  it.each([
    { accepted: false }, { build: { ...build, buildHash: 'dev' } }, { build: { ...build, sandboxVersion: 'old' } },
    { sandboxOrigin: 'https://other.test' }, { version: 1 },
  ])('destroys its frame on a rejected/mismatched handshake: %j', async (change) => {
    const { promise } = await creation({ ...accepted, ...change });
    await expect(promise).rejects.toMatchObject({ code: 'sandbox_build_mismatch' });
    expect(document.querySelector('iframe')).toBeNull();
    expect(getActiveVaultSandboxClient()).toBeNull();
  });

  it('destroys a wrong-build ready frame without sending handshake or sensitive RPC', async () => {
    const { promise, post } = await creation(accepted, { ...ready, build: { ...build, buildHash: 'dev' } });
    await expect(promise).rejects.toMatchObject({ code: 'sandbox_build_mismatch' });
    expect(post).not.toHaveBeenCalled();
    expect(document.querySelector('iframe')).toBeNull();
  });

  it('reset destroys an in-flight ready listener/frame and the next creation verifies again', async () => {
    const pending = getVaultSandboxClient();
    const rejected = expect(pending).rejects.toMatchObject({ code: 'sandbox_reset' });
    await vi.waitFor(() => expect(document.querySelector('iframe')).not.toBeNull());
    resetVaultSandboxClient();
    await rejected;
    expect(document.querySelector('iframe')).toBeNull();
    const count = vi.mocked(fetch).mock.calls.length;
    const next = await creation();
    const client = await next.promise;
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(count);
    expect(getActiveVaultSandboxClient()).toBe(client);
  });

  it('a reset during fetch cannot navigate or clear a newer successful client', async () => {
    let resolve!: (response: Response) => void;
    vi.mocked(fetch).mockImplementationOnce(() => new Promise<Response>((done) => { resolve = done; }));
    const old = getVaultSandboxClient();
    const rejected = expect(old).rejects.toMatchObject({ code: 'sandbox_reset', retryable: true });
    resetVaultSandboxClient();
    const next = await creation();
    const client = await next.promise;
    resolve(releaseResponse(`${VAULT_SANDBOX_ORIGIN}${VAULT_SANDBOX_MANIFEST_PATH}`));
    await rejected;
    expect(document.querySelectorAll('iframe')).toHaveLength(1);
    expect(getActiveVaultSandboxClient()).toBe(client);
  });
});

describe('inert parent preopen', () => {
  it('reserves activation synchronously, then navigates only after hashes pass with fragments only', async () => {
    const popup = stubBlankPopup();
    const handle = openVaultAuthorizationWindow()!;
    expect(window.open).toHaveBeenCalledWith('about:blank', '_blank', 'popup,width=520,height=760');
    expect(popup.location.replace).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(popup.location.replace).toHaveBeenCalledOnce());
    const url = new URL(popup.location.replace.mock.calls[0][0]);
    expect(url.origin + url.pathname).toBe(VAULT_SANDBOX_IFRAME_URL);
    expect(url.search).toBe('');
    expect(new URLSearchParams(url.hash.slice(1)).get('id')).toBe(handle.id);
    handle.close();
    expect(popup.closed).toBe(true);
  });

  it.each(['failure', 'close', 'reset', 'user-close'])('never navigates after %s and cleans only its popup', async (mode) => {
    let resolve!: (response: Response) => void;
    vi.mocked(fetch).mockImplementationOnce(() => new Promise<Response>((done) => { resolve = done; }));
    const popup = stubBlankPopup();
    const handle = openVaultAuthorizationWindow()!;
    if (mode === 'close') handle.close();
    if (mode === 'reset') resetVaultSandboxClient();
    if (mode === 'user-close') popup.closed = true;
    const response = mode === 'failure' ? new Response('bad', { status: 503 }) : releaseResponse(`${VAULT_SANDBOX_ORIGIN}${VAULT_SANDBOX_MANIFEST_PATH}`);
    resolve(response);
    await vi.waitFor(() => expect(popup.closed).toBe(true));
    await new Promise((done) => setTimeout(done, 20));
    expect(popup.location.replace).not.toHaveBeenCalled();
    expect(document.querySelector('iframe')).toBeNull();
  });

  it('does not send an authorization RPC if the popup failed verification', async () => {
    const { promise, post } = await creation();
    const client = await promise;
    stubBlankPopup();
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError('offline'));
    const handle = openVaultAuthorizationWindow()!;
    await expect(client.approveRelease({ items: [] }, handle.id)).rejects.toMatchObject({ code: 'sandbox_integrity_failed' });
    expect(post.mock.calls.some(([message]) => message.op === 'approveRelease')).toBe(false);
    handle.close();
  });
});
