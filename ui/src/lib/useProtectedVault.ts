import { useCallback, useEffect, useReducer, useState } from 'react';

import {
  useApi,
  type VaultWebAuthnRegistrationPayload,
  type VaultWebAuthnSerializedCredential,
} from '@/context/ApiContext';
import {
  getVaultSandboxClient,
  type DaemonAgentBinding,
  type VaultSandboxSealResult,
  type VaultSandboxSigningContext,
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

export type ProtectedVaultStatus = 'checking' | 'needs-setup' | 'locked' | 'unlocked' | 'error';

const VAULT_AUTO_LOCK_MS = 10 * 60 * 1000;

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

let vaultLockExpiresAt: number | null = null;
let vaultAutoLockTimer: ReturnType<typeof setTimeout> | null = null;
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

function autoLockExpired(): boolean {
  return vaultLockExpiresAt != null && Date.now() >= vaultLockExpiresAt;
}

export function vaultUnlocked(): boolean {
  if (sessionVault.status === 'unlocked' && autoLockExpired()) {
    clearUnlockState();
    queueMicrotask(notifyVaultLockChange);
    void getVaultSandboxClient()
      .then((client) => client.lock())
      .catch(() => undefined);
    return false;
  }
  return sessionVault.status === 'unlocked';
}

function enforceAutoLock(): boolean {
  if (sessionVault.status === 'unlocked' && autoLockExpired()) {
    void lockVault(false);
    return false;
  }
  return sessionVault.status === 'unlocked';
}

export function vaultFreshSetup(): boolean {
  return sessionVault.freshSetup;
}

function vaultStatusNow(): ProtectedVaultStatus {
  return sessionVault.status;
}

function clearUnlockState(): void {
  if (vaultAutoLockTimer) {
    clearTimeout(vaultAutoLockTimer);
    vaultAutoLockTimer = null;
  }
  vaultLockExpiresAt = null;
  if (sessionVault.freshSetup) {
    sessionVault.wrapMeta = null;
    sessionVault.freshSetup = false;
    sessionVault.authzFactorRegistration = null;
    sessionVault.status = 'needs-setup';
  } else {
    sessionVault.status = sessionVault.wrapMeta ? 'locked' : 'needs-setup';
  }
}

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

function armVaultAutoLock(expiresAt?: number | null): void {
  getVaultLockChannel();
  const sandboxDeadline = typeof expiresAt === 'number' && Number.isFinite(expiresAt) ? expiresAt : null;
  vaultLockExpiresAt = sandboxDeadline ?? Date.now() + VAULT_AUTO_LOCK_MS;
  if (vaultAutoLockTimer) clearTimeout(vaultAutoLockTimer);
  vaultAutoLockTimer = setTimeout(() => void lockVault(false), Math.max(0, vaultLockExpiresAt - Date.now()));
  notifyVaultLockChange();
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
  armVaultAutoLock(expiresAt);
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
    enforceAutoLock();
    if (sessionVault.status === 'unlocked') {
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
          armVaultAutoLock(sandboxStatus.expiresAt);
        } else {
          sessionVault.status = 'locked';
        }
      } else {
        sessionVault.wrapMeta = null;
        sessionVault.status = 'needs-setup';
      }
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
    ): Promise<
      VaultSandboxSealResult & {
        authzFactorRegistration?: VaultWebAuthnRegistrationPayload;
      }
    > => {
      if (!enforceAutoLock()) throw new Error('vault-locked');
      const wrapMeta = sessionVault.wrapMeta;
      if (!wrapMeta) throw new Error('vault-locked');
      armVaultAutoLock();
      const sealed = await (await getVaultSandboxClient()).seal({
        name,
        kind,
        inputMode: 'sandbox-entry',
        wrapMeta,
      });
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

  const signProtectedRequest = useCallback(
    async (
      material: ProtectedUnlockMaterial,
      signingContext: VaultSandboxSigningContext,
      scheme: SignatureScheme,
    ): Promise<SignatureResult> => {
      if (!enforceAutoLock()) throw new Error('vault-locked');
      armVaultAutoLock();
      return (await getVaultSandboxClient()).sign({ material, scheme, signingContext });
    },
    [],
  );

  const releaseProtectedDelivery = useCallback(
    async (material: ProtectedUnlockMaterial, agentBinding: DaemonAgentBinding): Promise<BlindBox> => {
      if (!enforceAutoLock()) throw new Error('vault-locked');
      armVaultAutoLock();
      return (await getVaultSandboxClient()).releaseDEK({ material, agentBinding });
    },
    [],
  );

  const lock = useCallback(() => {
    void lockVault(true);
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
    releaseProtectedDelivery,
    afterCreated,
    lock,
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
    setNow(Date.now());
    const id = setInterval(() => {
      enforceAutoLock();
      setNow(Date.now());
    }, 1000);
    return () => clearInterval(id);
  }, [unlocked]);

  const expiresAt = vaultUnlockExpiresAt();
  const remainingMs = expiresAt ? Math.max(0, expiresAt - now) : 0;
  return { unlocked, remainingMs, lockNow: () => void lockVault(true) };
}
