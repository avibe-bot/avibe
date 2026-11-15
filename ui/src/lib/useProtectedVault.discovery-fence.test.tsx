// @vitest-environment jsdom
// Protected Vault discovery is shared by every surface in a tab, and its reads can finish in any
// order. A read that started before a newer fact (a committed setup, an adopted server root, a
// discard, a lock) must never apply over it; every setup conflict adopts the server's real root.
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  createVaultAuthzWebAuthnOptions: vi.fn(),
  getVaultSandboxRootMetadata: vi.fn(),
  getVaultVmk: vi.fn(),
  completeVaultSetup: vi.fn(),
}));
const sandbox = vi.hoisted(() => ({
  setup: vi.fn(),
  status: vi.fn(),
  seal: vi.fn(),
  sign: vi.fn(),
  lock: vi.fn(),
  sealHdWallet: vi.fn(),
}));
const getClient = vi.hoisted(() => vi.fn());
const resetClient = vi.hoisted(() => vi.fn());

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('./vaultEntry', () => ({ assertVaultEntryAvailable: vi.fn(), vaultEntryIssue: () => null }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: getClient,
  resetVaultSandboxClient: resetClient,
}));

const passkeyRoot = (credentialId: string) => JSON.stringify({
  v: 1,
  rp_id: 'sandbox.avibe.bot',
  copies: [{ kind: 'passkey', kdf: 'webauthn-prf-hkdf-sha256', credential_id: credentialId }],
});
const SERVER_ROOT = passkeyRoot('AQID');
const LOSING_ROOT = passkeyRoot('BAUG');
// How the sandbox labels a record sealed under `root`: the root plus `scheme` and the record's DEK.
const recordUnder = (root: string) => JSON.stringify({ ...JSON.parse(root), scheme: 'machine-aesgcm-v1', dek_nonce: 'd', wrapped_dek: 'k' });
// The same root as the server may serialize it: the same fields in another order.
const reordered = (root: string) => {
  const { copies, ...rest } = JSON.parse(root) as Record<string, unknown>;
  return JSON.stringify({ copies, ...Object.fromEntries(Object.entries(rest).reverse()) });
};
const registration = {
  challenge_id: 'wch_1',
  credential: { id: 'AQID', rawId: 'AQID', type: 'public-key', response: { clientDataJSON: 'e30=', attestationObject: 'AA==' } },
};

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void };
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
// Let every continuation of an answered request run.
const settle = () => act(async () => { await new Promise((done) => setTimeout(done, 0)); });

let vault: typeof import('./useProtectedVault');

beforeEach(async () => {
  vi.stubGlobal('BroadcastChannel', undefined);
  api.createVaultAuthzWebAuthnOptions.mockReset().mockResolvedValue({ ok: true });
  api.getVaultSandboxRootMetadata.mockReset().mockResolvedValue({ ok: true, root_metadata: {} });
  api.getVaultVmk.mockReset().mockResolvedValue({ ok: true, exists: false });
  api.completeVaultSetup.mockReset().mockResolvedValue({ ok: true, exists: true, wrap_meta: SERVER_ROOT });
  sandbox.setup.mockReset().mockResolvedValue({ wrapMeta: SERVER_ROOT, authzRegistration: registration, state: 'unlocked' });
  sandbox.status.mockReset().mockResolvedValue({ state: 'locked' });
  sandbox.seal.mockReset().mockResolvedValue({ envelope: { ciphertext: 'c', nonce: 'n', wrap_meta: SERVER_ROOT }, establishingVmk: false });
  sandbox.sign.mockReset().mockResolvedValue({ signature: '01' });
  sandbox.lock.mockReset().mockResolvedValue(undefined);
  sandbox.sealHdWallet.mockReset();
  getClient.mockReset().mockResolvedValue(sandbox);
  // A fresh sandbox client holds no key, so it reports locked.
  resetClient.mockReset().mockImplementation(() => { sandbox.status.mockResolvedValue({ state: 'locked' }); });
  // Every test starts from a page that has not discovered anything yet.
  vi.resetModules();
  vault = await import('./useProtectedVault');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('a discovery read that started before setup was committed (single tab)', () => {
  // The user opens the protected dialog, closes it while it is still checking, reopens it and sets
  // the Vault up. The first dialog's read is still out; it must not undo the setup when it lands.
  it.each([
    {
      ending: 'a late exists:false',
      arrange: () => {
        const old = deferred<unknown>();
        api.getVaultVmk.mockReturnValueOnce(old.promise);
        return () => old.resolve({ ok: true, exists: false });
      },
    },
    {
      ending: 'a late discovery failure',
      arrange: () => {
        const old = deferred<unknown>();
        api.getVaultVmk.mockReturnValueOnce(old.promise);
        return () => old.reject(new Error('offline'));
      },
    },
    {
      ending: 'exists:false answered early, but its sandbox client arriving late',
      arrange: () => {
        const client = deferred<typeof sandbox>();
        api.getVaultVmk.mockResolvedValueOnce({ ok: true, exists: false });
        getClient.mockReturnValueOnce(client.promise);
        return () => client.resolve(sandbox);
      },
    },
  ])('keeps the committed root after $ending', async ({ arrange }) => {
    const finishOldRead = arrange();
    const closed = renderHook(() => vault.useProtectedVault());
    act(() => { void closed.result.current.refresh(); });
    closed.unmount();

    const reopened = renderHook(() => vault.useProtectedVault());
    await act(async () => { await reopened.result.current.refresh(); });
    expect(reopened.result.current.status).toBe('needs-setup');
    await act(async () => { await reopened.result.current.setupPasskey(); });
    expect(reopened.result.current.status).toBe('unlocked');

    finishOldRead();
    await settle();
    expect(reopened.result.current.status).toBe('unlocked');
    expect(reopened.result.current.error).toBeNull();
    expect(vault.vaultUnlocked()).toBe(true);

    // The first protected save seals under that durable root and sends no registration again.
    const sealed = await reopened.result.current.sealValue('TOKEN', 'static', { valueRef: { current: 'redacted' }, clear: vi.fn() });
    expect(sandbox.seal).toHaveBeenCalledWith(expect.objectContaining({ wrapMeta: SERVER_ROOT }));
    expect(sealed.authzFactorRegistration).toBeUndefined();
    expect(sandbox.setup).toHaveBeenCalledOnce();
    expect(api.completeVaultSetup).toHaveBeenCalledOnce();
  });
});

describe('every setup conflict adopts the root the server already has', () => {
  it.each([
    {
      trigger: 'the shared helper, when registration options show a Passkey exists',
      run: async () => {
        api.createVaultAuthzWebAuthnOptions.mockResolvedValue({ ok: true, requires_existing_factor: true });
        await expect(vault.setupVaultDurably(api)).resolves.toEqual({ ok: false, code: 'vault_already_initialized' });
        expect(sandbox.setup).not.toHaveBeenCalled();
      },
    },
    {
      trigger: 'the shared helper, when the setup commit loses to another tab',
      run: async () => {
        sandbox.setup.mockResolvedValue({ wrapMeta: LOSING_ROOT, authzRegistration: registration, state: 'unlocked' });
        api.completeVaultSetup.mockResolvedValue({ ok: false, code: 'vault_already_initialized' });
        await expect(vault.setupVaultDurably(api)).resolves.toEqual({ ok: false, code: 'vault_already_initialized' });
        expect(sandbox.setup).toHaveBeenCalledOnce();
      },
    },
    {
      trigger: 'a protected create refused because the Vault was set up elsewhere',
      run: async (hook: { current: ReturnType<typeof vault.useProtectedVault> }) => {
        await hook.current.discardAndRefresh(LOSING_ROOT);
      },
    },
  ])('$trigger, even when an older empty read lands afterwards', async ({ run }) => {
    const old = deferred<unknown>();
    api.getVaultVmk.mockReturnValueOnce(old.promise).mockResolvedValue({ ok: true, exists: true, wrap_meta: SERVER_ROOT });
    const observer = renderHook(() => vault.useProtectedVault());
    act(() => { void observer.result.current.refresh(); });

    await act(async () => { await run(observer.result); });
    expect(vault.vaultFreshSetup()).toBe(false);
    expect(observer.result.current.hasPasskey()).toBe(true);

    old.resolve({ ok: true, exists: false });
    await settle();
    expect(observer.result.current.status).toBe('locked');
    expect(observer.result.current.hasPasskey()).toBe(true);
    expect(vault.vaultUnlocked()).toBe(false);
  });
});

it('drops the refused setup key from the sandbox even when the server root was adopted while its commit was out', async () => {
  const commit = deferred<unknown>();
  sandbox.setup.mockResolvedValue({ wrapMeta: LOSING_ROOT, authzRegistration: registration, state: 'unlocked' });
  api.completeVaultSetup.mockReturnValue(commit.promise);
  const hook = renderHook(() => vault.useProtectedVault());
  let setup!: Promise<void>;
  act(() => { setup = hook.result.current.setupPasskey(); });
  await vi.waitFor(() => expect(api.completeVaultSetup).toHaveBeenCalledOnce());

  // Another surface discovers the root another browser committed. The sandbox still holds the
  // refused setup's key, and its `status` does not tell roots apart: it answers unlocked.
  api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: SERVER_ROOT });
  sandbox.status.mockResolvedValue({ state: 'unlocked', expiresAt: Date.now() + 60_000 });
  const surface = renderHook(() => vault.useProtectedVault());
  await act(async () => { await surface.result.current.refresh(); });

  const resetsBefore = resetClient.mock.calls.length;
  commit.resolve({ ok: false, code: 'vault_already_initialized' });
  await act(async () => { await expect(setup).rejects.toMatchObject({ code: 'vault_already_initialized' }); });
  expect(resetClient).toHaveBeenCalledTimes(resetsBefore + 1);
  expect(vault.vaultUnlocked()).toBe(false);
  expect(surface.result.current.status).toBe('locked');
  expect(surface.result.current.hasPasskey()).toBe(true);
});

describe('a root named by a record, or serialized in another key order, is the same root', () => {
  it('keeps the sandbox client (and a ceremony on it) when discovery finds the root a record was opened under', async () => {
    api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: SERVER_ROOT });
    sandbox.status.mockResolvedValue({ state: 'unlocked', expiresAt: Date.now() + 60_000 });
    const card = renderHook(() => vault.useProtectedVault());
    await act(async () => { await card.result.current.refresh(); });
    const material = { name: 'KEY', envelope: { ciphertext: 'c', nonce: 'n', wrap_meta: recordUnder(SERVER_ROOT) } };
    await act(async () => {
      await card.result.current.signProtectedRequest(material, {} as never, 'ecdsa-secp256k1' as never, {} as never);
    });
    await settle();

    // Locked again; another protected surface mounts and discovers the same durable root.
    act(() => { card.result.current.lock(); });
    sandbox.status.mockResolvedValue({ state: 'locked' });
    api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: reordered(SERVER_ROOT) });
    const next = renderHook(() => vault.useProtectedVault());
    await act(async () => { await next.result.current.refresh(); });
    expect(resetClient).not.toHaveBeenCalled();
    expect(next.result.current.status).toBe('locked');
  });

  it('a protected save refused with the root it sealed under drops that root and discovers the durable one', async () => {
    api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: SERVER_ROOT });
    sandbox.status.mockResolvedValue({ state: 'unlocked', expiresAt: Date.now() + 60_000 });
    const form = renderHook(() => vault.useProtectedVault());
    await act(async () => { await form.result.current.refresh(); });
    expect(vault.vaultUnlocked()).toBe(true);
    const reads = api.getVaultVmk.mock.calls.length;

    api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: LOSING_ROOT });
    sandbox.status.mockResolvedValue({ state: 'locked' });
    await act(async () => { await form.result.current.discardAndRefresh(recordUnder(SERVER_ROOT)); });
    expect(api.getVaultVmk).toHaveBeenCalledTimes(reads + 1);
    expect(vault.vaultUnlocked()).toBe(false);
    expect(form.result.current.status).toBe('locked');
  });
});

describe('discovery after an unanswered setup commit', () => {
  // The commit got no answer, so this tab keeps its setup and the sandbox keeps that root's key
  // unlocked. The sandbox `status` answers unlocked for any root while it holds a key.
  it.each([
    { found: 'another root committed elsewhere', serverRoot: SERVER_ROOT, resets: 1, unlocked: false, status: 'locked' },
    { found: 'this setup, committed after all', serverRoot: LOSING_ROOT, resets: 0, unlocked: true, status: 'unlocked' },
  ])('finding $found', async ({ serverRoot, resets, unlocked, status }) => {
    sandbox.setup.mockResolvedValue({ wrapMeta: LOSING_ROOT, authzRegistration: registration, state: 'unlocked' });
    api.completeVaultSetup.mockRejectedValueOnce(new Error('offline'));
    sandbox.status.mockResolvedValue({ state: 'unlocked', expiresAt: Date.now() + 60_000 });
    const hook = renderHook(() => vault.useProtectedVault());
    await act(async () => {
      await expect(hook.result.current.setupPasskey()).rejects.toMatchObject({ code: 'network_error' });
    });

    api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: serverRoot });
    await act(async () => { await hook.result.current.refresh(); });
    // A key for another root is never treated as this root's: nothing could open what it seals.
    expect(resetClient).toHaveBeenCalledTimes(resets);
    expect(vault.vaultUnlocked()).toBe(unlocked);
    expect(hook.result.current.status).toBe(status);
    expect(vault.vaultFreshSetup()).toBe(false);
  });
});

it('keeps an unanswered setup for an identical retry when an older empty read lands', async () => {
  const old = deferred<unknown>();
  api.getVaultVmk.mockReturnValueOnce(old.promise);
  api.completeVaultSetup.mockRejectedValueOnce(new Error('offline'));
  // The sandbox keeps the uncommitted root unlocked, whatever this page mirrors.
  sandbox.status.mockResolvedValue({ state: 'unlocked', expiresAt: Date.now() + 60_000 });
  const hook = renderHook(() => vault.useProtectedVault());
  act(() => { void hook.result.current.refresh(); });

  await act(async () => {
    await expect(hook.result.current.setupPasskey()).rejects.toMatchObject({ code: 'network_error' });
  });
  old.resolve({ ok: true, exists: false });
  await settle();
  expect(vault.vaultFreshSetup()).toBe(true);
  expect(hook.result.current.status).toBe('needs-setup');

  await act(async () => { await hook.result.current.setupPasskey(); });
  expect(sandbox.setup).toHaveBeenCalledOnce();
  expect(api.completeVaultSetup).toHaveBeenCalledTimes(2);
  expect(api.completeVaultSetup.mock.calls[1]).toEqual(api.completeVaultSetup.mock.calls[0]);
  expect(hook.result.current.status).toBe('unlocked');
});

it('shows the newest discovery failure, and a superseded refresh resolves without an error', async () => {
  const old = deferred<unknown>();
  api.getVaultVmk.mockReturnValueOnce(old.promise).mockRejectedValueOnce(new Error('vmk-discovery-failed'));
  const hook = renderHook(() => vault.useProtectedVault());
  let superseded!: Promise<void>;
  act(() => { superseded = hook.result.current.refresh(); });
  await act(async () => { await hook.result.current.refresh(); });
  expect(hook.result.current.status).toBe('error');

  old.resolve({ ok: true, exists: false });
  await act(async () => { await expect(superseded).resolves.toBeUndefined(); });
  expect(hook.result.current.status).toBe('error');
  expect(hook.result.current.error).toBe('vmk-discovery-failed');
});

describe('a sandbox status answer that was asked before the user locked', () => {
  it.each([
    {
      source: 'discovery',
      start: (hook: { current: ReturnType<typeof vault.useProtectedVault> }) => {
        api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: SERVER_ROOT });
        void hook.current.refresh();
      },
    },
    {
      source: 'a finished protected signature',
      start: async (hook: { current: ReturnType<typeof vault.useProtectedVault> }) => {
        api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: SERVER_ROOT });
        sandbox.status.mockResolvedValueOnce({ state: 'unlocked', expiresAt: Date.now() + 60_000 });
        await hook.current.refresh();
        expect(vault.vaultUnlocked()).toBe(true);
        const material = { name: 'KEY', envelope: { ciphertext: 'c', nonce: 'n', wrap_meta: SERVER_ROOT } };
        await hook.current.signProtectedRequest(material, {} as never, 'ecdsa-secp256k1' as never, {} as never);
      },
    },
  ])('does not unlock again after a $source', async ({ start }) => {
    const answer = deferred<unknown>();
    const hook = renderHook(() => vault.useProtectedVault());
    sandbox.status.mockImplementation(() => answer.promise);
    await act(async () => {
      await start(hook.result);
      await vi.waitFor(() => expect(sandbox.status).toHaveBeenCalled());
    });

    act(() => { hook.result.current.lock(); });
    answer.resolve({ state: 'unlocked', expiresAt: Date.now() + 60_000 });
    await settle();
    expect(vault.vaultUnlocked()).toBe(false);
    expect(hook.result.current.status).toBe('locked');
    expect(hook.result.current.hasPasskey()).toBe(true);
  });
});

it('refuses a protected seed-phrase wallet while setup is not yet committed, before any sandbox work', async () => {
  const commit = deferred<unknown>();
  api.completeVaultSetup.mockReturnValue(commit.promise);
  const hook = renderHook(() => vault.useProtectedVault());
  let setup!: Promise<void>;
  act(() => { setup = hook.result.current.setupPasskey(); });
  await vi.waitFor(() => expect(api.completeVaultSetup).toHaveBeenCalledOnce());

  const clientsBefore = getClient.mock.calls.length;
  await expect(hook.result.current.sealHdWallet('COLD', 'create', 'win1')).rejects.toThrow('vault-setup-pending');
  expect(getClient).toHaveBeenCalledTimes(clientsBefore);
  expect(sandbox.sealHdWallet).not.toHaveBeenCalled();
  expect(api.completeVaultSetup).toHaveBeenCalledOnce();

  commit.resolve({ ok: true, exists: true, wrap_meta: SERVER_ROOT });
  await act(async () => { await setup; });
});
