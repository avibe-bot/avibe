import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { vaultErrorMessage } from './vaultErrors';

import {
  useApi,
  type ApiContextType,
  type VaultSignedOperationContext,
  type VaultWebAuthnRegistrationPayload,
  type VaultWebAuthnSerializedCredential,
} from '@/context/ApiContext';
import {
  getVaultSandboxClient,
  PROTECTED_HD_OP,
  resetVaultSandboxClient,
  subscribeVaultStateEvents,
  type ApproveReleaseItem,
  type VaultSandboxSealResult,
  type VaultSandboxSignScheme,
  type VaultSandboxSigningContext,
  type VaultSandboxStatusResult,
  type VaultStateEvent,
  VaultSandboxError,
} from './vaultSandboxClient';
import { type BlindBox, type ProtectedRecordEnvelope, type SignatureResult } from './vaultCrypto';
import { assertVaultEntryAvailable, vaultEntryIssue } from './vaultEntry';
import { invalidateVaultSandboxPolicy } from './vaultSandboxPolicy';
import type { ProtectedHdSealResult } from './vaultProtectedHd';

/**
 * Value-free protected material hydrated into UI approval cards. The parent app brokers it to the
 * sandbox; the VMK, DEK, private key, and plaintext stay inside sandbox.avibe.bot.
 */
export type ProtectedUnlockMaterial = {
  name: string;
  field?: string;
  kind?: string | null;
  envelope: ProtectedRecordEnvelope;
};

type ParentStaticSealValue = {
  valueRef: { current: string };
  clear: () => void;
};

export type ProtectedVaultStatus = 'checking' | 'needs-setup' | 'locked' | 'unlocked' | 'error';

const sessionVault: {
  status: Exclude<ProtectedVaultStatus, 'checking' | 'error'>;
  wrapMeta: string | null;
  freshSetup: boolean;
  authzFactorRegistration: VaultWebAuthnRegistrationPayload | null;
  /** The sandbox root exists only in memory until POST /api/vault/setup succeeds. */
  durableSetupPending: boolean;
} = {
  status: 'needs-setup',
  wrapMeta: null,
  freshSetup: false,
  authzFactorRegistration: null,
  durableSetupPending: false,
};

// Reads finish in any order, and every surface in this tab shares the state above. An async read
// notes these counters when it starts and applies its answer only if nothing newer was learned
// meanwhile: `rootEpoch` moves when what this tab knows about the Vault root changes (a setup, a
// commit, a discovered or discarded root), `lockEpoch` when the lock mirror changes.
let rootEpoch = 0;
let lockEpoch = 0;

// The root whose key this tab's sandbox client may hold (it set up or unlocked under it). The
// sandbox answers `status` unlocked for any root while it holds a key, so a different root must
// never be asked about on that client: a seal there would label a record with a root it cannot open.
let sandboxRoot: string | null = null;

function dropSandboxClient(): void {
  sandboxRoot = null;
  resetVaultSandboxClient();
}

/** The sandbox client to ask about `root`: a fresh one if the current one may hold another root's key. */
function sandboxClientFor(root: string) {
  if (sandboxRoot !== null && sandboxRoot !== root) dropSandboxClient();
  return getVaultSandboxClient();
}

/** Every change to what this tab knows about the Vault root goes through here. */
function writeRoot(
  wrapMeta: string | null,
  freshSetup = false,
  authzFactorRegistration: VaultWebAuthnRegistrationPayload | null = null,
  durableSetupPending = false,
): void {
  if (
    sessionVault.wrapMeta !== wrapMeta
    || sessionVault.freshSetup !== freshSetup
    || sessionVault.authzFactorRegistration !== authzFactorRegistration
    || sessionVault.durableSetupPending !== durableSetupPending
  ) {
    rootEpoch += 1;
  }
  sessionVault.wrapMeta = wrapMeta;
  sessionVault.freshSetup = freshSetup;
  sessionVault.authzFactorRegistration = authzFactorRegistration;
  sessionVault.durableSetupPending = durableSetupPending;
}

// The unlock-window deadline (ms epoch) is now owned by the sandbox — the parent only *mirrors*
// the value it receives from `vault.state` events and `status` results (protocol v2 §6.4/§8).
// There is no parent-side auto-lock timer anymore: the sandbox holds the single clock and emits a
// `vault.state { locked, auto-lock }` event when it expires. See the module subscription below.
let vaultLockExpiresAt: number | null = null;
const vaultLockListeners = new Set<() => void>();

let vaultLockChannel: BroadcastChannel | null = null;
let vaultLockChannelInit = false;
function getVaultLockChannel(): BroadcastChannel | null {
  if (vaultLockChannelInit) return vaultLockChannel;
  vaultLockChannelInit = true;
  if (typeof BroadcastChannel === 'undefined') return null;
  vaultLockChannel = new BroadcastChannel('avibe-vault-lock');
  vaultLockChannel.onmessage = (event: MessageEvent) => {
    if (event.data === 'lock') void lockVault(false);
    // A policy tightening (enabling Strict) in another tab: lock AND drop this tab's sandbox client
    // so it re-handshakes under the new policy — a plain lock would leave this tab's client pinned
    // to the stale policy, and its next auto-unlock would run non-Strict.
    else if (event.data === 'reset') applyPolicyResetLock();
  };
  return vaultLockChannel;
}

function notifyVaultLockChange(): void {
  for (const listener of [...vaultLockListeners]) listener();
}

export function subscribeVaultLock(listener: () => void): () => void {
  vaultLockListeners.add(listener);
  return () => {
    vaultLockListeners.delete(listener);
  };
}

export function vaultUnlockExpiresAt(): number | null {
  return vaultUnlocked() ? vaultLockExpiresAt : null;
}

export function vaultUnlocked(): boolean {
  // The sandbox is authoritative: it auto-locks and emits a `vault.state` event we mirror, so the
  // parent no longer second-guesses the clock. A rendered countdown reaching 0 is cosmetic; the
  // real transition arrives as an event (or the next `status` reconciliation).
  return sessionVault.status === 'unlocked' && !sessionVault.durableSetupPending;
}

export function vaultFreshSetup(): boolean {
  return sessionVault.freshSetup;
}

function vaultStatusNow(): ProtectedVaultStatus {
  return sessionVault.durableSetupPending ? 'needs-setup' : sessionVault.status;
}

function assertDurableSetupReady(): void {
  if (sessionVault.durableSetupPending) throw new Error('vault-setup-pending');
}

function hasPendingSetupProof(): boolean {
  return Boolean(
    sessionVault.durableSetupPending
    && sessionVault.freshSetup
    && sessionVault.wrapMeta
    && sessionVault.authzFactorRegistration,
  );
}

function clearUnlockState(options: { discardPendingSetup?: boolean } = {}): void {
  vaultLockExpiresAt = null;
  lockEpoch += 1;
  // Ordinary sandbox lock/unload events must not destroy the exact tuple needed to retry an
  // unresolved setup POST. Explicit abandonment or a final server refusal opts into discard.
  if (hasPendingSetupProof() && !options.discardPendingSetup) {
    sessionVault.status = 'needs-setup';
    return;
  }
  if (sessionVault.freshSetup) {
    // A fresh setup that never persisted a secret: locking reverts to needs-setup (the daemon has
    // no wrap_meta yet), so drop the in-memory handle rather than pretend a vault exists.
    writeRoot(null);
    sessionVault.status = 'needs-setup';
  } else {
    writeRoot(sessionVault.wrapMeta);
    sessionVault.status = sessionVault.wrapMeta ? 'locked' : 'needs-setup';
  }
}

// The single source of truth for lock state: sandbox `vault.state` events. One module-level
// subscription mirrors every transition (unlock / renew / auto-lock / manual-lock / unload) into
// the shared session state and notifies React subscribers. Registered once at module load so it is
// live before any component mounts and survives sandbox-client recreation.
function applyVaultStateEvent(event: VaultStateEvent): void {
  if (event.state === 'unlocked') {
    // The sandbox only unlocks material the parent handed it, so wrapMeta should already be known;
    // guard anyway so a stray event can't flip us to a wrapMeta-less "unlocked".
    if (!sessionVault.wrapMeta) return;
    lockEpoch += 1;
    sandboxRoot = sessionVault.wrapMeta;
    sessionVault.status = 'unlocked';
    if (typeof event.expiresAt === 'number' && Number.isFinite(event.expiresAt)) vaultLockExpiresAt = event.expiresAt;
    if (!sessionVault.durableSetupPending) notifyVaultLockChange();
    return;
  }
  clearUnlockState();
  notifyVaultLockChange();
}

subscribeVaultStateEvents(applyVaultStateEvent);

async function lockVault(broadcast = false): Promise<void> {
  clearUnlockState();
  notifyVaultLockChange();
  if (broadcast) getVaultLockChannel()?.postMessage('lock');
  try {
    await (await getVaultSandboxClient()).lock();
  } catch {
    // Local parent state is already locked; a best-effort sandbox lock failure remains fail-closed.
  }
}

// Lock this tab AND discard its sandbox client so the next ceremony re-handshakes under the fresh
// policy. Used for a policy tightening (enabling Strict): the sandbox pins policy at handshake and
// its internal auto-unlock reuses that pin, so locking alone would let this tab re-unlock stale.
function applyPolicyResetLock(): void {
  clearUnlockState();
  notifyVaultLockChange();
  // Invalidate the parent policy mirror too: the reset is a tightening, so if the next handshake's
  // settings fetch fails we must fail closed (strict) rather than re-pin the old confirmed policy.
  invalidateVaultSandboxPolicy();
  dropSandboxClient();
}

// Broadcast a policy reset to every tab (including this one): all drop their pinned sandbox client
// and re-handshake under the new policy on the next ceremony. This is the cross-tab counterpart to
// the local reset — a plain `lock` broadcast would leave siblings re-unlocking under stale policy.
function lockAndResetAllTabs(): void {
  getVaultLockChannel()?.postMessage('reset');
  applyPolicyResetLock();
}

function baseVmkWrapMeta(wrapMeta: string): string {
  const parsed = JSON.parse(wrapMeta) as Record<string, unknown>;
  delete parsed.dek_nonce;
  delete parsed.wrapped_dek;
  delete parsed.record_meta;
  return JSON.stringify(parsed);
}

function hasPasskeyCopy(wrapMeta: string | null): boolean {
  if (!wrapMeta) return false;
  try {
    const meta = JSON.parse(wrapMeta) as { copies?: Array<{ kind?: string }> };
    return Array.isArray(meta.copies) && meta.copies.some((copy) => copy?.kind === 'passkey');
  } catch {
    return false;
  }
}

function commitUnlocked(
  wrapMeta: string,
  freshSetup: boolean,
  expiresAt?: number | null,
  authzFactorRegistration: VaultWebAuthnRegistrationPayload | null = null,
  options: { deferNotification?: boolean } = {},
): void {
  writeRoot(baseVmkWrapMeta(wrapMeta), freshSetup, authzFactorRegistration, Boolean(options.deferNotification));
  sessionVault.status = 'unlocked';
  sandboxRoot = sessionVault.wrapMeta;
  lockEpoch += 1;
  // A setup root is not usable by the create flow until its root+factor POST has answered. Keep the
  // sandbox material for a same-proof retry, but do not notify hooks or expose an unlocked state.
  // Mirror the sandbox-provided deadline directly; the event stream keeps it fresh on renewal.
  vaultLockExpiresAt = typeof expiresAt === 'number' && Number.isFinite(expiresAt) ? expiresAt : null;
  getVaultLockChannel();
  if (!options.deferNotification) notifyVaultLockChange();
}

/**
 * Mirror a sandbox `status` answer for the known durable root. A lock transition after the question
 * was asked is newer, so it stays; no answer (the sandbox did not reply) is locked, never unlocked.
 */
function mirrorSandboxStatus(answer: VaultSandboxStatusResult | null, askedAt: number): void {
  if (askedAt !== lockEpoch) {
    if (sessionVault.status === 'needs-setup') sessionVault.status = 'locked';
    return;
  }
  lockEpoch += 1;
  if (answer?.state === 'unlocked') {
    sessionVault.status = 'unlocked';
    sandboxRoot = sessionVault.wrapMeta;
    vaultLockExpiresAt = typeof answer.expiresAt === 'number' && Number.isFinite(answer.expiresAt) ? answer.expiresAt : null;
  } else {
    sessionVault.status = 'locked';
    vaultLockExpiresAt = null;
  }
}

type DiscoveryApi = Pick<ApiContextType, 'getVaultVmk'>;
type Discovery = { outcome: 'applied' | 'superseded' } | { outcome: 'failed'; error: unknown };

/**
 * Ask the server which root is durable and the sandbox whether it is unlocked, then apply both, unless
 * this tab learned something newer about the root meanwhile (then the answer is dropped: it is older).
 */
async function discoverVaultRoot(api: DiscoveryApi): Promise<Discovery> {
  const rootAt = rootEpoch;
  try {
    const res = await api.getVaultVmk();
    if (!res?.ok) throw new Error('vmk-discovery-failed');
    await getVaultSandboxClient();
    // A sandbox client now exists for this tab (even while locked); make sure the cross-tab lock
    // channel is live so a policy `reset` broadcast reaches this tab and drops its pinned client.
    getVaultLockChannel();
    if (rootAt !== rootEpoch) return { outcome: 'superseded' };
    if (res.exists && res.wrap_meta) {
      const root = baseVmkWrapMeta(res.wrap_meta);
      const askedAt = lockEpoch;
      let answer: VaultSandboxStatusResult | null = null;
      let statusError: unknown = null;
      try {
        answer = await (await sandboxClientFor(root)).status(root);
      } catch (error) {
        statusError = error ?? new Error('vmk-discovery-failed');
      }
      if (rootAt !== rootEpoch) return { outcome: 'superseded' };
      // The server's root is durable (an unknown setup response may have committed it): it is no
      // longer a transient setup, and it stays known (locked) even if the sandbox did not answer.
      writeRoot(root);
      mirrorSandboxStatus(answer, askedAt);
      notifyVaultLockChange();
      return statusError ? { outcome: 'failed', error: statusError } : { outcome: 'applied' };
    }
    // A setup POST may still be retryable after an unknown response: keep its exact root and
    // registration rather than discard them because discovery ran before the commit landed.
    if (!hasPendingSetupProof()) {
      writeRoot(null);
      sessionVault.status = 'needs-setup';
      vaultLockExpiresAt = null;
      lockEpoch += 1;
    }
    // Wake every `subscribeVaultLock` listener (e.g. sibling VaultLockIndicators), not just the
    // caller: the shared state is what changed.
    notifyVaultLockChange();
    return { outcome: 'applied' };
  } catch (error) {
    if (rootAt !== rootEpoch) return { outcome: 'superseded' };
    sessionVault.status = sessionVault.wrapMeta ? 'locked' : 'needs-setup';
    lockEpoch += 1;
    return { outcome: 'failed', error };
  }
}

/** A setup lost to a root that is already durable: adopt that root, so this page offers unlock. */
async function adoptCommittedRoot(api: DiscoveryApi): Promise<{ ok: false; code: string }> {
  await discoverVaultRoot(api);
  return { ok: false, code: 'vault_already_initialized' };
}

function isSerializedCredential(value: unknown): value is VaultWebAuthnSerializedCredential {
  return typeof value === 'object' && value != null && 'rawId' in value && 'response' in value;
}

function registrationFromSandbox(value: unknown): VaultWebAuthnRegistrationPayload | null {
  if (typeof value !== 'object' || value == null) return null;
  const candidate = value as Partial<VaultWebAuthnRegistrationPayload>;
  if (typeof candidate.challenge_id === 'string' && isSerializedCredential(candidate.credential)) {
    return candidate as VaultWebAuthnRegistrationPayload;
  }
  return null;
}

type SetupApi = Pick<ApiContextType, 'createVaultAuthzWebAuthnOptions' | 'getVaultSandboxRootMetadata'>;

/** The sandbox setup ceremony: a new Passkey and Vault root, held in this tab until something
 *  persists it (the first protected key, or {@link setupVaultDurably}). */
async function runPasskeySetup(
  api: SetupApi,
  options: { onlyFirstFactor?: boolean; deferNotification?: boolean } = {},
): Promise<void> {
  const sandbox = await getVaultSandboxClient();
  const [authzOptions, rootMetadata] = await Promise.all([
    api.createVaultAuthzWebAuthnOptions(),
    api.getVaultSandboxRootMetadata(),
  ]);
  if (!authzOptions?.ok) throw new Error(authzOptions?.code || 'passkey-registration-options-failed');
  if (!rootMetadata?.ok) throw new Error(rootMetadata?.code || 'sandbox-root-metadata-failed');
  // The server already has a Passkey (set up in another tab meanwhile, or a commit whose reply was
  // lost): never create a second credential behind it.
  if (options.onlyFirstFactor && authzOptions.requires_existing_factor) {
    throw Object.assign(new Error('vault-already-initialized'), { code: 'vault_already_initialized' });
  }
  const result = await sandbox.setup({
    vaultUserHandle: 'avibe-vault',
    displayName: 'Avibe Vault',
    existingProtectedVault: Boolean(sessionVault.wrapMeta),
    authzCreationOptions: authzOptions,
    rootMetadata: rootMetadata.root_metadata,
  });
  const authzRegistration = registrationFromSandbox(result.authzRegistration);
  commitUnlocked(result.wrapMeta, true, result.expiresAt, authzRegistration, { deferNotification: options.deferNotification });
}

/**
 * Set up a Passkey without adding a protected key (contract v1.1 §G): the sandbox ceremony, then
 * `POST /api/vault/setup` commits the root and its authorization factor durably. Afterwards this
 * tab is an ordinary set-up Vault: the first protected key reuses that root and sends no
 * registration again (the server matches the root and its active factor), and locking keeps it.
 * Setting up is not consent to anything else; turning a confirmation off still asks separately.
 */
export async function setupVaultDurably(
  api: SetupApi & DiscoveryApi & Pick<ApiContextType, 'completeVaultSetup'>,
): Promise<{ ok: true } | { ok: false; code: string }> {
  // Only after the server said this instance was never set up (`vault_policy_setup_required`): a
  // root or factor that exists must never get a second credential enrolled behind it. A setup this
  // tab made but could not commit yet is committed again as it is: no second ceremony.
  const pendingFresh = sessionVault.freshSetup && sessionVault.wrapMeta && sessionVault.authzFactorRegistration;
  if (sessionVault.wrapMeta && !pendingFresh) return { ok: false, code: 'vault_already_initialized' };
  if (!pendingFresh) {
    try {
      await runPasskeySetup(api, { onlyFirstFactor: true, deferNotification: true });
    } catch (error) {
      // The server already has a Passkey: adopt its root, for every caller of this helper.
      if ((error as { code?: unknown } | null)?.code === 'vault_already_initialized') return adoptCommittedRoot(api);
      throw error;
    }
  }
  const wrapMeta = sessionVault.wrapMeta;
  const registration = sessionVault.authzFactorRegistration;
  if (!wrapMeta || !registration) {
    clearUnlockState();
    notifyVaultLockChange();
    return { ok: false, code: 'setup_incomplete' };
  }
  const committed = await api.completeVaultSetup({ wrap_meta: wrapMeta, authz_factor_registration: registration })
    .catch(() => ({ ok: false as const, code: 'network_error' }));
  // Whether this tab still holds exactly the setup it posted (an explicit discard may have dropped it).
  const stillPosted = sessionVault.durableSetupPending
    && sessionVault.wrapMeta === wrapMeta
    && sessionVault.authzFactorRegistration === registration;
  if (!committed.ok) {
    // No answer (network) or no reason given: it may still have landed, so a retry posts this same
    // setup again (no second ceremony). Any reason the server gives is final for this setup: another
    // tab committed first, or this registration is refused (expired after 2 minutes, invalid). Then
    // drop the uncommitted root, here and in the sandbox (its iframe still holds it unlocked), so the
    // next attempt starts a fresh ceremony and discovery finds whatever root is durable.
    const final = typeof committed.code === 'string' && committed.code !== 'network_error';
    if (final) {
      // The sandbox still holds the refused root's VMK unlocked, even if this page has meanwhile
      // learned another root (its `status` does not tell roots apart): always drop that client, so
      // nothing is sealed under the refused key, and lock the mirror. The page's setup is discarded
      // only if it is still the one that was posted.
      dropSandboxClient();
      clearUnlockState({ discardPendingSetup: stillPosted });
      notifyVaultLockChange();
    }
    // Another tab committed first: adopt the root it committed, for every caller of this helper.
    if (committed.code === 'vault_already_initialized') return adoptCommittedRoot(api);
    return { ok: false, code: committed.code || 'setup_failed' };
  }
  // Durable now. This answer is newer than any discovery read that started before it, and adopting
  // it moves the root epoch, so such a read can no longer apply its older answer.
  const root = typeof committed.wrap_meta === 'string' ? baseVmkWrapMeta(committed.wrap_meta) : wrapMeta;
  const sandboxUnlocked = stillPosted && root === wrapMeta && sessionVault.status === 'unlocked';
  writeRoot(root);
  if (!sandboxUnlocked) {
    // The root is durable even if status reconciliation is unavailable; fail closed and let the
    // normal unlock path re-query the sandbox rather than claiming the VMK is available.
    sessionVault.status = 'locked';
    vaultLockExpiresAt = null;
    lockEpoch += 1;
    const rootAt = rootEpoch;
    const askedAt = lockEpoch;
    const answer = await sandboxClientFor(root)
      .then((sandbox) => sandbox.status(root))
      .catch(() => null);
    if (rootAt === rootEpoch) mirrorSandboxStatus(answer, askedAt);
  }
  notifyVaultLockChange();
  return { ok: true };
}

export function useProtectedVault() {
  const api = useApi();
  const { t } = useTranslation();
  const [status, setStatus] = useState<ProtectedVaultStatus>(vaultStatusNow());
  const [error, setError] = useState<string | null>(null);
  // Only this hook's newest refresh settles its own status and error.
  const latestRefresh = useRef(0);

  useEffect(() => {
    // Initialize the cross-tab lock channel as soon as any vault UI mounts, so this tab receives
    // `reset`/`lock` broadcasts regardless of which path later creates a sandbox client (reveal,
    // approve, sign, and seal all acquire the client directly, not only refresh()). Idempotent.
    getVaultLockChannel();
    return subscribeVaultLock(() => {
      setStatus((prev) => (prev === 'checking' || prev === 'error' ? prev : vaultStatusNow()));
    });
  }, []);

  const refresh = useCallback(async () => {
    const call = ++latestRefresh.current;
    // Even direct hook callers must stop before discovery/preparation, including
    // a stale mirrored unlocked state left by another surface.
    try {
      assertVaultEntryAvailable();
    } catch (error) {
      setStatus('error');
      setError(vaultErrorMessage(t, error, t('errors.sandbox_entry_unavailable')));
      return;
    }
    if (sessionVault.status === 'unlocked' && !sessionVault.durableSetupPending) {
      // Already unlocked and kept honest by the event stream — don't flash a "checking" state or
      // re-query (a `status` call is silent but pointless here).
      setStatus('unlocked');
      return;
    }
    setStatus('checking');
    setError(null);
    const discovery = await discoverVaultRoot(api);
    // A newer refresh of this hook settles it instead. A superseded one resolves quietly: rejecting
    // would surface an outdated error through its caller.
    if (call !== latestRefresh.current) return;
    if (discovery.outcome === 'failed') {
      setStatus('error');
      setError(vaultErrorMessage(t, discovery.error, 'vmk-discovery-failed'));
      return;
    }
    setStatus(vaultStatusNow());
  }, [api, t]);

  const setupPasskey = useCallback(async () => {
    // A conflict has already adopted the committed root (in the shared helper), so the open dialog
    // offers unlock instead of a setup that can only conflict again.
    const result = await setupVaultDurably(api);
    setStatus(vaultStatusNow());
    if (!result.ok) throw Object.assign(new Error(result.code), { code: result.code });
    setError(null);
  }, [api]);

  const unlockPasskey = useCallback(async () => {
    assertDurableSetupReady();
    assertVaultEntryAvailable();
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) throw new Error('vault-not-setup');
    const result = await (await getVaultSandboxClient()).unlock({ wrapMeta });
    commitUnlocked(result.wrapMeta || wrapMeta, false, result.expiresAt);
    setStatus('unlocked');
    setError(null);
  }, []);

  const sealValue = useCallback(
    async (
      name: string,
      kind: 'static' | 'keypair' = 'static',
      parentStaticValue?: ParentStaticSealValue,
      /** A keypair is secp256k1 unless this asks for an Ed25519 (Solana) key. */
      curve?: 'ed25519',
    ): Promise<
      VaultSandboxSealResult & {
        authzFactorRegistration?: VaultWebAuthnRegistrationPayload;
      }
    > => {
      assertVaultEntryAvailable();
      assertDurableSetupReady();
      const wrapMeta = sessionVault.wrapMeta;
      if (!wrapMeta) throw new Error('vault-locked');
      const sandbox = await getVaultSandboxClient();
      let sealed: VaultSandboxSealResult;
      if (kind === 'static') {
        if (!parentStaticValue) throw new Error('protected-static-value-required');
        // Hand the plaintext to the sandbox, then drop the parent-held copy IMMEDIATELY. `seal()`
        // captures `value` into the request payload synchronously here, so we clear the persistent
        // ref + revealed field right away rather than leaving plaintext in parent memory for the
        // whole ceremony (R1 seal is silent while unlocked, but the value must not linger).
        const sealing = sandbox.seal({ name, kind: 'static', value: parentStaticValue.valueRef.current, wrapMeta });
        parentStaticValue.valueRef.current = '';
        parentStaticValue.clear();
        sealed = await sealing;
      } else {
        // Keypair is generate-only: the key is born inside the sandbox and never crosses the
        // boundary; the parent receives only ciphertext + public key/addresses (protocol v2 §7.2).
        sealed = await sandbox.seal({ name, kind: 'keypair', ...(curve ? { curve } : {}), wrapMeta });
      }
      return {
        ...sealed,
        authzFactorRegistration: sessionVault.freshSetup
          ? (sessionVault.authzFactorRegistration ?? undefined)
          : undefined,
      };
    },
    [],
  );

  /**
   * Make or import a protected seed-phrase wallet in the sandbox window opened by the caller's click
   * (contract rev0.7 §4). The vault must exist (locked is fine: the window unlocks it itself); the
   * window gets only the name, the mode and this vault's base root.
   */
  const sealHdWallet = useCallback(
    async (
      name: string,
      mode: 'create' | 'import',
      authorizationWindow: string,
    ): Promise<ProtectedHdSealResult & { authzFactorRegistration?: VaultWebAuthnRegistrationPayload }> => {
      assertVaultEntryAvailable();
      assertDurableSetupReady();
      const wrapMeta = sessionVault.wrapMeta;
      if (!wrapMeta) throw new Error('vault-not-setup');
      const freshSetup = sessionVault.freshSetup;
      const registration = sessionVault.authzFactorRegistration;
      const sandbox = await getVaultSandboxClient();
      const sealed = await sandbox.sealHdWallet({ name, mode, wrapMeta }, authorizationWindow);
      // The page's setup/root snapshot must still be the one the window used (rev0.6 §4): a reset or
      // a new setup in between drops the result here, before anything is sent to Avibe. The window's
      // own establishment flag is sent as it reports it, as every protected seal does (the shared
      // setup lifecycle lets it stay true after the root is committed; the daemon reconciles it).
      if (sessionVault.wrapMeta !== wrapMeta || sessionVault.freshSetup !== freshSetup
        || (freshSetup && sessionVault.authzFactorRegistration !== registration)) {
        throw new VaultSandboxError('sandbox_reset', 'The protected vault changed while the wallet was being made', true);
      }
      return { ...sealed, authzFactorRegistration: freshSetup ? (registration ?? undefined) : undefined };
    },
    [],
  );

  const afterCreated = useCallback(() => {
    if (sessionVault.durableSetupPending) return;
    writeRoot(sessionVault.wrapMeta);
  }, []);

  const syncProtectedOperationStatus = useCallback(async (wrapMeta: string) => {
    if (sessionVault.durableSetupPending) return;
    const rootAt = rootEpoch;
    const askedAt = lockEpoch;
    // The sandbox has just opened this record, so a key it holds is this record's root's.
    const sandbox = await getVaultSandboxClient();
    const baseWrapMeta = baseVmkWrapMeta(wrapMeta);
    const answer = await sandbox.status(baseWrapMeta);
    // A root learned or dropped since this operation started (a new setup, a discard) is newer.
    if (rootAt !== rootEpoch) {
      setStatus(vaultStatusNow());
      return;
    }
    writeRoot(baseWrapMeta);
    mirrorSandboxStatus(answer, askedAt);
    getVaultLockChannel();
    notifyVaultLockChange();
    setStatus(vaultStatusNow());
    if (vaultUnlocked()) setError(null);
  }, []);

  const signProtectedRequest = useCallback(
    async (
      material: ProtectedUnlockMaterial,
      signingContext: VaultSandboxSigningContext,
      scheme: VaultSandboxSignScheme,
      context: VaultSignedOperationContext,
      authorizationWindow?: string,
    ): Promise<SignatureResult> => {
      assertDurableSetupReady();
      const sandbox = await getVaultSandboxClient();
      try {
        // Approval cards include display metadata (kind/field); the sign RPC accepts
        // only the record identity and ciphertext envelope, not that UI metadata.
        return await sandbox.sign({
          material: { name: material.name, envelope: material.envelope },
          scheme, signingContext, context,
        }, authorizationWindow);
      } finally {
        void syncProtectedOperationStatus(material.envelope.wrap_meta).catch(() => undefined);
      }
    },
    [syncProtectedOperationStatus],
  );

  /**
   * Batch DEK release for an access approval: one confirm card covers every protected member and
   * the sandbox returns one HPKE blind box per item (order matches `items`). Replaces the v1
   * per-secret `releaseProtectedDelivery` loop.
   */
  const approveProtectedRelease = useCallback(
    async (items: ApproveReleaseItem[], authorizationWindow?: string): Promise<BlindBox[]> => {
      if (items.length === 0) return [];
      assertDurableSetupReady();
      const sandbox = await getVaultSandboxClient();
      try {
        const result = await sandbox.approveRelease({ items }, authorizationWindow);
        return result.blindBoxes;
      } finally {
        const wrapMeta = items[0]?.material.envelope.wrap_meta;
        if (wrapMeta) void syncProtectedOperationStatus(wrapMeta).catch(() => undefined);
      }
    },
    [syncProtectedOperationStatus],
  );

  /**
   * Reveal a protected static value inside the sandbox frame. Plaintext is displayed (and, on the
   * sandbox-side explicit action, copied) entirely within the sandbox — it is never returned to
   * the parent. R2: an in-sandbox confirm while unlocked, a passkey while locked or under Strict.
   */
  const revealProtectedValue = useCallback(
    async (
      material: ProtectedUnlockMaterial,
      context: VaultSignedOperationContext,
      authorizationWindow?: string,
    ): Promise<{ completed: boolean }> => {
      assertDurableSetupReady();
      const sandbox = await getVaultSandboxClient();
      try {
        return await sandbox.reveal({ material, context }, authorizationWindow);
      } finally {
        void syncProtectedOperationStatus(material.envelope.wrap_meta).catch(() => undefined);
      }
    },
    [syncProtectedOperationStatus],
  );

  const lock = useCallback(() => {
    void lockVault(true);
    setStatus(vaultStatusNow());
  }, []);

  // Lock + force a sandbox re-handshake across ALL tabs. Used when a policy tightening (Strict)
  // must take effect immediately everywhere: the sandbox pins policy at handshake, so every tab
  // must drop its client and re-handshake under the new policy, not merely lock.
  const lockAndResetForPolicyChange = useCallback(() => {
    lockAndResetAllTabs();
    setStatus(vaultStatusNow());
  }, []);

  /**
   * A protected save was refused because the Vault was set up elsewhere: drop the root that save
   * used (`rejectedRoot`, else whatever this tab holds) and discover the durable one. A root this
   * tab learned since that save is kept. Either way, no read that started earlier can apply.
   */
  const discardAndRefresh = useCallback(async (rejectedRoot?: string) => {
    const rejected = rejectedRoot ? baseVmkWrapMeta(rejectedRoot) : sessionVault.wrapMeta;
    if (rejected === sessionVault.wrapMeta) {
      clearUnlockState({ discardPendingSetup: true });
      writeRoot(null);
      sessionVault.status = 'needs-setup';
      notifyVaultLockChange();
    }
    rootEpoch += 1;
    await refresh();
  }, [refresh]);

  const hasPasskey = useCallback(() => !sessionVault.durableSetupPending && hasPasskeyCopy(sessionVault.wrapMeta), []);

  const passkeyUsableHere = useCallback(() => !sessionVault.durableSetupPending && hasPasskeyCopy(sessionVault.wrapMeta), []);

  return {
    status,
    error,
    setError,
    refresh,
    setupPasskey,
    unlockPasskey,
    sealValue,
    sealHdWallet,
    signProtectedRequest,
    approveProtectedRelease,
    revealProtectedValue,
    afterCreated,
    lock,
    lockAndResetForPolicyChange,
    discardAndRefresh,
    hasPasskey,
    passkeyUsableHere,
  };
}

/**
 * Starts the sandbox client while `enabled` and reports whether it is ready. Its first build
 * fetches, hashes, and handshakes, and a protected step whose click opens the authorization window
 * must not do that after the click (protocol v2 §6.6), so the click waits for this. A failed build
 * also reports ready, so the step itself surfaces the error.
 */
export function useVaultSandboxWarm(enabled: boolean): boolean {
  const allowed = enabled && !vaultEntryIssue();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    if (!allowed) return;
    let alive = true;
    const settle = () => {
      if (alive) setReady(true);
    };
    getVaultSandboxClient().then(settle, settle);
    return () => {
      alive = false;
    };
  }, [allowed]);
  return !allowed || ready;
}

export function useVaultLock(): { unlocked: boolean; remainingMs: number; lockNow: () => void } {
  const [, forceRender] = useReducer((n: number) => n + 1, 0);
  useEffect(() => subscribeVaultLock(forceRender), []);

  const unlocked = vaultUnlocked();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!unlocked) return;
    // Pure render clock: tick `now` once a second so the countdown advances. The sandbox owns the
    // actual auto-lock and emits a `vault.state` event when it fires — the parent never locks off
    // this timer (protocol v2 §8). The 0ms timer syncs `now` right after unlock without a
    // setState directly in the effect body (which would trigger a cascading render).
    const sync = () => setNow(Date.now());
    const immediate = window.setTimeout(sync, 0);
    const id = setInterval(sync, 1000);
    return () => {
      window.clearTimeout(immediate);
      clearInterval(id);
    };
  }, [unlocked]);

  const expiresAt = vaultUnlockExpiresAt();
  const remainingMs = expiresAt ? Math.max(0, expiresAt - now) : 0;
  return { unlocked, remainingMs, lockNow: () => void lockVault(true) };
}

/** Whether the pinned sandbox build can create protected seed-phrase wallets; null while unknown. */
export function useProtectedHdSupported(enabled: boolean): boolean | null {
  const allowed = enabled && !vaultEntryIssue();
  const [supported, setSupported] = useState<boolean | null>(null);
  useEffect(() => {
    if (!allowed) return;
    let alive = true;
    getVaultSandboxClient()
      .then((client) => client.supports(PROTECTED_HD_OP))
      .then((value) => { if (alive) setSupported(value); }, () => { if (alive) setSupported(false); });
    return () => {
      alive = false;
    };
  }, [allowed]);
  return allowed ? supported : null;
}
