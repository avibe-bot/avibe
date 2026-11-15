// @vitest-environment jsdom
// The sequence 3.2.6 shipped with: another tab or device finished setting up the Vault (root B)
// while this tab ran its own Passkey ceremony (root A). This tab's first protected save is refused
// with vault_already_initialized, and the user saves again without reloading. That second save
// must be refused or bound to B; it must never seal a record with A's key, which B's Passkey can
// never open.
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const passkeyRoot = (credentialId: string) => JSON.stringify({
  v: 1,
  rp_id: 'sandbox.avibe.bot',
  copies: [{ kind: 'passkey', kdf: 'webauthn-prf-hkdf-sha256', credential_id: credentialId }],
});
const ROOT_A = passkeyRoot('AAAA');
const ROOT_B = passkeyRoot('BBBB');
const rootOf = (wrapMeta: string) => (JSON.parse(wrapMeta) as { copies: Array<{ credential_id: string }> }).copies[0].credential_id;

// A sandbox that behaves as the real one does: once set up it holds that root's key, and `status`
// answers unlocked for any root while it holds a key. A seal uses the held key and labels the record
// with the root it was asked for (plus scheme and its own DEK fields).
const box = vi.hoisted(() => ({
  heldKey: null as string | null,
  records: [] as Array<{ label: string; key: string }>,
}));
const sandbox = vi.hoisted(() => ({
  setup: vi.fn(),
  status: vi.fn(),
  seal: vi.fn(),
  lock: vi.fn(),
}));
const api = vi.hoisted(() => ({
  createVaultAuthzWebAuthnOptions: vi.fn(),
  getVaultSandboxRootMetadata: vi.fn(),
  getVaultVmk: vi.fn(),
  completeVaultSetup: vi.fn(),
}));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('./vaultEntry', () => ({ assertVaultEntryAvailable: vi.fn(), vaultEntryIssue: () => null }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: async () => sandbox,
  resetVaultSandboxClient: () => { box.heldKey = null; },
}));

beforeEach(() => {
  vi.stubGlobal('BroadcastChannel', undefined);
  box.heldKey = null;
  box.records = [];
  // Registration options were fetched before the other device committed B.
  api.createVaultAuthzWebAuthnOptions.mockResolvedValue({ ok: true });
  api.getVaultSandboxRootMetadata.mockResolvedValue({ ok: true, root_metadata: {} });
  api.getVaultVmk.mockResolvedValue({ ok: true, exists: true, wrap_meta: ROOT_B });
  api.completeVaultSetup.mockResolvedValue({ ok: false, code: 'vault_already_initialized' });
  sandbox.setup.mockImplementation(async () => {
    box.heldKey = rootOf(ROOT_A);
    return {
      wrapMeta: ROOT_A,
      state: 'unlocked',
      authzRegistration: { challenge_id: 'wch_1', credential: { id: 'AAAA', rawId: 'AAAA', type: 'public-key', response: { clientDataJSON: 'e30=', attestationObject: 'AA==' } } },
    };
  });
  sandbox.status.mockImplementation(async () => (box.heldKey ? { state: 'unlocked', expiresAt: Date.now() + 60_000 } : { state: 'locked' }));
  sandbox.seal.mockImplementation(async ({ wrapMeta }: { wrapMeta: string }) => {
    if (!box.heldKey) throw Object.assign(new Error('locked'), { code: 'vault_locked' });
    box.records.push({ label: rootOf(wrapMeta), key: box.heldKey });
    const record = JSON.stringify({ ...JSON.parse(wrapMeta), scheme: 'machine-aesgcm-v1', dek_nonce: 'd', wrapped_dek: 'k' });
    return { envelope: { ciphertext: 'c', nonce: 'n', wrap_meta: record }, establishingVmk: rootOf(wrapMeta) === 'AAAA' };
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('after a save refused because the Vault was set up elsewhere, the next save never seals with the dead root key', async () => {
  const { useProtectedVault } = await import('./useProtectedVault');
  const form = renderHook(() => useProtectedVault());
  const value = () => ({ valueRef: { current: 'redacted' }, clear: vi.fn() });
  // The create dialog saves only while its protected step shows unlocked.
  const save = async (name: string) => {
    if (form.result.current.status !== 'unlocked') return null;
    return form.result.current.sealValue(name, 'static', value());
  };

  // This tab sets up its Passkey; another device has already committed B.
  await act(async () => { await form.result.current.setupPasskey().catch(() => undefined); });

  // First save: the server refuses it (vault_already_initialized), and the dialog drops the
  // refused root and rediscovers, as the form does on that code.
  await act(async () => {
    const first = await save('FIRST');
    if (first) {
      const discard = form.result.current.discardAndRefresh as (rejectedRoot?: string) => Promise<void>;
      await discard(first.envelope.wrap_meta);
    }
  });

  // Second save, without a reload.
  await act(async () => { await save('SECOND').catch(() => null); });

  const wrongKey = box.records.filter((record) => record.label !== record.key);
  expect(wrongKey).toEqual([]);
  expect(box.records.some((record) => record.key === 'AAAA' && record.label === 'BBBB')).toBe(false);
});
