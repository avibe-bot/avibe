import { useCallback, useEffect, useReducer, useState } from 'react';

import {
  useApi,
  type VaultSignedOperationContext,
  type VaultWebAuthnRegistrationPayload,
  type VaultWebAuthnSerializedCredential,
} from '@/context/ApiContext';
import {
  getVaultSandboxClient,
  resetVaultSandboxClient,
  subscribeVaultStateEvents,
  type ApproveReleaseItem,
  type VaultSandboxSealResult,
  type VaultSandboxSigningContext,
  type VaultStateEvent,
} from './vaultSandboxClient';
import { type BlindBox, type ProtectedRecordEnvelope, type SignatureResult, type SignatureScheme } from './vaultCrypto';

/**
 * Value-free protected material hydrated into UI approval cards. The parent app brokers it to the
 * sandbox; the VMK, DEK, private key, and plaintext stay inside sandbox.avibe.bot.
 */
export type ProtectedUnlockMaterial = {
  name: string;
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
} = {
  status: 'needs-setup',
  wrapMeta: null,
  freshSetup: false,
  authzFactorRegistration: null,
};

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
  return sessionVault.status === 'unlocked' ? vaultLockExpiresAt : null;
}

export function vaultUnlocked(): boolean {
  // The sandbox is authoritative: it auto-locks and emits a `vault.state` event we mirror, so the
  // parent no longer second-guesses the clock. A rendered countdown reaching 0 is cosmetic; the
  // real transition arrives as an event (or the next `status` reconciliation).
  return sessionVault.status === 'unlocked';
}

export function vaultFreshSetup(): boolean {
  return sessionVault.freshSetup;
}

function vaultStatusNow(): ProtectedVaultStatus {
  return sessionVault.status;
}

function clearUnlockState(): void {
  vaultLockExpiresAt = null;
  if (sessionVault.freshSetup) {
    // A fresh setup that never persisted a secret: locking reverts to needs-setup (the daemon has
    // no wrap_meta yet), so drop the in-memory handle rather than pretend a vault exists.
    sessionVault.wrapMeta = null;
    sessionVault.freshSetup = false;
    sessionVault.authzFactorRegistration = null;
    sessionVault.status = 'needs-setup';
  } else {
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
    sessionVault.status = 'unlocked';
    if (typeof event.expiresAt === 'number' && Number.isFinite(event.expiresAt)) vaultLockExpiresAt = event.expiresAt;
    notifyVaultLockChange();
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
  resetVaultSandboxClient();
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
): void {
  sessionVault.wrapMeta = baseVmkWrapMeta(wrapMeta);
  sessionVault.status = 'unlocked';
  sessionVault.freshSetup = freshSetup;
  sessionVault.authzFactorRegistration = authzFactorRegistration;
  // Mirror the sandbox-provided deadline directly; the event stream keeps it fresh on renewal.
  vaultLockExpiresAt = typeof expiresAt === 'number' && Number.isFinite(expiresAt) ? expiresAt : null;
  getVaultLockChannel();
  notifyVaultLockChange();
}

function commitLocked(wrapMeta: string): void {
  vaultLockExpiresAt = null;
  sessionVault.wrapMeta = baseVmkWrapMeta(wrapMeta);
  sessionVault.status = 'locked';
  sessionVault.freshSetup = false;
  sessionVault.authzFactorRegistration = null;
  notifyVaultLockChange();
}

export function webauthnAvailable(): boolean {
  return typeof window !== 'undefined' && typeof crypto !== 'undefined' && Boolean(crypto.subtle);
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

export function useProtectedVault() {
  const api = useApi();
  const [status, setStatus] = useState<ProtectedVaultStatus>(sessionVault.status);
  const [error, setError] = useState<string | null>(null);

  useEffect(
    () =>
      subscribeVaultLock(() => {
        setStatus((prev) => (prev === 'checking' || prev === 'error' ? prev : vaultStatusNow()));
      }),
    [],
  );

  const refresh = useCallback(async () => {
    if (sessionVault.status === 'unlocked') {
      // Already unlocked and kept honest by the event stream — don't flash a "checking" state or
      // re-query (a `status` call is silent but pointless here).
      setStatus('unlocked');
      return;
    }
    setStatus('checking');
    setError(null);
    try {
      const res = await api.getVaultVmk();
      if (!res?.ok) throw new Error('vmk-discovery-failed');
      const sandbox = await getVaultSandboxClient();
      if (res.exists && res.wrap_meta) {
        sessionVault.wrapMeta = baseVmkWrapMeta(res.wrap_meta);
        const sandboxStatus = await sandbox.status(sessionVault.wrapMeta);
        if (sandboxStatus.state === 'unlocked') {
          sessionVault.status = 'unlocked';
          vaultLockExpiresAt =
            typeof sandboxStatus.expiresAt === 'number' && Number.isFinite(sandboxStatus.expiresAt) ? sandboxStatus.expiresAt : null;
          getVaultLockChannel();
        } else {
          sessionVault.status = 'locked';
          vaultLockExpiresAt = null;
        }
      } else {
        sessionVault.wrapMeta = null;
        sessionVault.status = 'needs-setup';
        vaultLockExpiresAt = null;
      }
      // Reconciling an existing sandbox session mutates the shared module state, so wake every
      // `subscribeVaultLock` listener (e.g. sibling VaultLockIndicators) — not just this hook's
      // `setStatus` below. The deleted `armVaultAutoLock()` path used to do this; without it a
      // remount that finds an already-unlocked session leaves siblings on stale lock state until
      // the next `vault.state` event.
      notifyVaultLockChange();
      setStatus(sessionVault.status);
    } catch (err) {
      sessionVault.status = sessionVault.wrapMeta ? 'locked' : 'needs-setup';
      setStatus('error');
      setError(err instanceof Error ? err.message : 'vmk-discovery-failed');
    }
  }, [api]);

  const setupPasskey = useCallback(async () => {
    const sandbox = await getVaultSandboxClient();
    const [authzOptions, rootMetadata] = await Promise.all([
      api.createVaultAuthzWebAuthnOptions(),
      api.getVaultSandboxRootMetadata(),
    ]);
    if (!authzOptions?.ok) throw new Error(authzOptions?.code || 'passkey-registration-options-failed');
    if (!rootMetadata?.ok) throw new Error(rootMetadata?.code || 'sandbox-root-metadata-failed');
    const result = await sandbox.setup({
      vaultUserHandle: 'avibe-vault',
      displayName: 'Avibe Vault',
      existingProtectedVault: Boolean(sessionVault.wrapMeta),
      authzCreationOptions: authzOptions,
      rootMetadata: rootMetadata.root_metadata,
    });
    const authzRegistration = registrationFromSandbox(result.authzRegistration);
    commitUnlocked(result.wrapMeta, true, result.expiresAt, authzRegistration);
    setStatus('unlocked');
    setError(null);
  }, [api]);

  const unlockPasskey = useCallback(async () => {
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
    ): Promise<
      VaultSandboxSealResult & {
        authzFactorRegistration?: VaultWebAuthnRegistrationPayload;
      }
    > => {
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
        // Keypair is generate-only: the secp256k1 key is born inside the sandbox and never crosses
        // the boundary; the parent receives only ciphertext + public key/addresses (protocol v2 §7.2).
        sealed = await sandbox.seal({ name, kind: 'keypair', wrapMeta });
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

  const afterCreated = useCallback(() => {
    sessionVault.freshSetup = false;
    sessionVault.authzFactorRegistration = null;
  }, []);

  const syncProtectedOperationStatus = useCallback(async (wrapMeta: string) => {
    const sandbox = await getVaultSandboxClient();
    const baseWrapMeta = baseVmkWrapMeta(wrapMeta);
    const sandboxStatus = await sandbox.status(baseWrapMeta);
    if (sandboxStatus.state === 'unlocked') {
      commitUnlocked(baseWrapMeta, false, sandboxStatus.expiresAt);
      setStatus('unlocked');
      setError(null);
      return;
    }
    commitLocked(wrapMeta);
    setStatus('locked');
  }, []);

  const signProtectedRequest = useCallback(
    async (
      material: ProtectedUnlockMaterial,
      signingContext: VaultSandboxSigningContext,
      scheme: SignatureScheme,
      context: VaultSignedOperationContext,
    ): Promise<SignatureResult> => {
      const sandbox = await getVaultSandboxClient();
      try {
        return await sandbox.sign({ material, scheme, signingContext, context });
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
    async (items: ApproveReleaseItem[]): Promise<BlindBox[]> => {
      if (items.length === 0) return [];
      const sandbox = await getVaultSandboxClient();
      try {
        const result = await sandbox.approveRelease({ items });
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
    async (material: ProtectedUnlockMaterial, context: VaultSignedOperationContext): Promise<{ completed: boolean }> => {
      const sandbox = await getVaultSandboxClient();
      try {
        return await sandbox.reveal({ material, context });
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

  const discardAndRefresh = useCallback(async () => {
    clearUnlockState();
    sessionVault.wrapMeta = null;
    sessionVault.freshSetup = false;
    sessionVault.authzFactorRegistration = null;
    notifyVaultLockChange();
    await refresh();
  }, [refresh]);

  const hasPasskey = useCallback(() => hasPasskeyCopy(sessionVault.wrapMeta), []);

  const passkeyUsableHere = useCallback(() => hasPasskeyCopy(sessionVault.wrapMeta), []);

  return {
    status,
    error,
    setError,
    refresh,
    setupPasskey,
    unlockPasskey,
    sealValue,
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
