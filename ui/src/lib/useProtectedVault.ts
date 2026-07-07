import { useCallback, useEffect, useReducer, useState } from 'react';

import {
  useApi,
  type ApiContextType,
  type VaultDeleteAuthz,
  type VaultDeleteChallengeResult,
  type VaultWebAuthnRegistrationPayload,
  type VaultWebAuthnSerializedCredential,
} from '@/context/ApiContext';
import {
  base64ToBytes,
  buildWrapMeta,
  bytesToBase64,
  newPasskeyPrfSalt,
  newVmk,
  packProtectedRecord,
  passkeyPrfSaltEntries,
  releaseProtectedDek,
  sealProtected,
  signProtectedDigest,
  unpackProtectedRecord,
  unwrapVmk,
  webAuthnPrfExtensionInput,
  type AvaultPublicKey,
  type BlindBox,
  type ProtectedDekDeliveryBlindBoxContext,
  type ProtectedRecordEnvelope,
  type SignatureResult,
  type SignatureScheme,
} from './vaultCrypto';

/**
 * The protected envelope + name the daemon hydrates onto a UI-audience request card
 * (`card.secret_unlock_material` / `scope_options[].unlock_material[]`). It is value-free
 * metadata; the browser opens it locally with the unlocked VMK to sign or release a DEK.
 */
export type ProtectedUnlockMaterial = {
  name: string;
  kind?: string | null;
  envelope: ProtectedRecordEnvelope;
};

/**
 * Protected-tier vault lifecycle for the Web UI.
 *
 * The Vault Master Key (VMK) lives only in browser memory and is wrapped by a
 * passkey (WebAuthn-PRF, Face ID / Touch ID / Windows Hello) into an opaque
 * `wrap_meta` the daemon stores per protected secret. The daemon never sees
 * the VMK or plaintext. No cross-origin sandbox yet — that hardening is a later
 * version.
 *
 * The unlocked VMK is cached at module scope so it survives `VaultSecretForm`
 * unmount/remount within one page session. A full reload re-initialises the module.
 */
export type ProtectedVaultStatus = 'checking' | 'needs-setup' | 'locked' | 'unlocked' | 'error';

const sessionVault: {
  vmk: Uint8Array | null;
  wrapMeta: string | null;
  freshSetup: boolean;
  authzFactorRegistration: VaultWebAuthnRegistrationPayload | null;
} = {
  vmk: null,
  wrapMeta: null,
  freshSetup: false,
  authzFactorRegistration: null,
};

/**
 * Auto-lock. The plaintext VMK is zeroed this long after it was unlocked (the timer is
 * refreshed on every use), shrinking the window it sits in browser memory from "until reload"
 * to a few idle minutes. A manual "Lock now" or a full page reload also clears it.
 */
const VAULT_AUTO_LOCK_MS = 10 * 60 * 1000;
let vaultLockExpiresAt: number | null = null;
let vaultAutoLockTimer: ReturnType<typeof setTimeout> | null = null;
const vaultLockListeners = new Set<() => void>();

/**
 * Cross-tab lock: a manual "Lock now" broadcasts here so every open tab for this origin wipes its
 * own cached VMK, not just the tab that clicked. Auto-lock stays per-tab (each tab's idle timer is
 * independent, so an idle tab locking shouldn't wipe a tab you're actively using).
 *
 * Created lazily on first unlock (never at module import): `BroadcastChannel` also exists in
 * Node/Vitest, and an open-at-import channel keeps the process alive and can hang the UI test run.
 */
let vaultLockChannel: BroadcastChannel | null = null;
let vaultLockChannelInit = false;
function getVaultLockChannel(): BroadcastChannel | null {
  if (vaultLockChannelInit) return vaultLockChannel;
  vaultLockChannelInit = true;
  if (typeof BroadcastChannel === 'undefined') return null;
  vaultLockChannel = new BroadcastChannel('avibe-vault-lock');
  vaultLockChannel.onmessage = (event: MessageEvent) => {
    if (event.data === 'lock') lockVault(false);
  };
  return vaultLockChannel;
}

function notifyVaultLockChange(): void {
  for (const listener of [...vaultLockListeners]) listener();
}

/** Subscribe to VMK unlock/auto-lock/lock transitions (module-global). Returns an unsubscribe. */
export function subscribeVaultLock(listener: () => void): () => void {
  vaultLockListeners.add(listener);
  return () => {
    vaultLockListeners.delete(listener);
  };
}

/** Wall-clock ms when the VMK auto-locks, or null while locked. */
export function vaultUnlockExpiresAt(): number | null {
  return sessionVault.vmk ? vaultLockExpiresAt : null;
}

/** True once the auto-lock wall-clock deadline has passed (independent of the setTimeout). */
function autoLockExpired(): boolean {
  return vaultLockExpiresAt != null && Date.now() >= vaultLockExpiresAt;
}

export function vaultUnlocked(): boolean {
  if (sessionVault.vmk && autoLockExpired()) {
    // Zero an expired VMK the instant anything checks the lock state, even if the (delayed) timer
    // hasn't fired — so no flow (delete gate, create/provision, approval) can treat a past-deadline
    // vault as unlocked.
    clearVmk();
    // Notify subscribers so other mounted forms/approval cards drop out of 'unlocked' too. Deferred
    // to a microtask because this may run during render (can't synchronously setState here).
    queueMicrotask(notifyVaultLockChange);
    return false;
  }
  return sessionVault.vmk != null;
}

/**
 * Enforce the auto-lock deadline synchronously. Browser timers fire late when a tab is
 * backgrounded/suspended or the machine sleeps, so `setTimeout(lockVault)` can lag well past the
 * deadline; call this before trusting or using the VMK to lock the moment the wall clock is past
 * due. Safe in event/effect contexts (it notifies). Returns whether the vault is still unlocked.
 */
function enforceAutoLock(): boolean {
  if (sessionVault.vmk && autoLockExpired()) {
    lockVault();
    return false;
  }
  return sessionVault.vmk != null;
}

/**
 * True when the in-memory VMK is a fresh, uncommitted first-time setup — NOT a proven unlock of
 * the server's established vault. In the first-init collision path (another tab established the
 * real vault after this tab set up but before it saved a secret) a fresh-setup VMK is a loser key
 * that must not be treated as authorization to act on a real protected secret.
 */
export function vaultFreshSetup(): boolean {
  return sessionVault.freshSetup;
}

function vaultStatusNow(): ProtectedVaultStatus {
  if (sessionVault.vmk) return 'unlocked';
  return sessionVault.wrapMeta ? 'locked' : 'needs-setup';
}

/** Zero the VMK and cancel the pending auto-lock. Does not notify — callers decide. */
function clearVmk(): void {
  sessionVault.vmk?.fill(0);
  sessionVault.vmk = null;
  vaultLockExpiresAt = null;
  if (vaultAutoLockTimer) {
    clearTimeout(vaultAutoLockTimer);
    vaultAutoLockTimer = null;
  }
  if (sessionVault.freshSetup) {
    // An uncommitted fresh VMK (set up but no protected secret saved yet) — drop it so a later
    // unlock can't seal under a stale local VMK that skips the atomic first-init guard. Done here
    // (not only in lockVault) so EVERY teardown path — manual lock, auto-lock, and the render-time
    // expiry in vaultUnlocked() — leaves a consistent state.
    sessionVault.wrapMeta = null;
    sessionVault.freshSetup = false;
    sessionVault.authzFactorRegistration = null;
  }
}

/** Lock the vault now: zero the VMK, drop an uncommitted fresh-setup VMK, notify subscribers. */
function lockVault(broadcast = false): void {
  clearVmk(); // zeros the VMK and drops any uncommitted fresh-setup state
  notifyVaultLockChange();
  // A manual "Lock now" wipes every open tab for this origin, not just this one.
  if (broadcast) getVaultLockChannel()?.postMessage('lock');
}

/** (Re)start the auto-lock countdown; call on unlock and on every VMK use. Notifies subscribers. */
function armVaultAutoLock(): void {
  if (!sessionVault.vmk) return;
  getVaultLockChannel(); // ensure this (unlocked) tab can receive a cross-tab "Lock now"
  vaultLockExpiresAt = Date.now() + VAULT_AUTO_LOCK_MS;
  if (vaultAutoLockTimer) clearTimeout(vaultAutoLockTimer);
  vaultAutoLockTimer = setTimeout(() => lockVault(), VAULT_AUTO_LOCK_MS);
  notifyVaultLockChange();
}

const WEBAUTHN_RP_NAME = 'Avibe Vault';
const WEBAUTHN_USER_HANDLE = new TextEncoder().encode('avibe-vault');

/**
 * WebAuthn needs a secure context and a domain RP ID. Browsers reject raw IP RP IDs
 * (the default local `http://127.0.0.1:5123` workflow), so protected vault setup and
 * unlock only work on `localhost` or an HTTPS domain (e.g. the tunnel).
 */
export function webauthnAvailable(): boolean {
  if (typeof window === 'undefined' || typeof window.PublicKeyCredential === 'undefined') return false;
  if (!window.isSecureContext) return false;
  const host = window.location.hostname;
  if (host === 'localhost') return true;
  if (host === '' || host.includes(':') || /^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return false; // IPv6/IPv4
  return host.includes('.');
}

/** A fresh ArrayBuffer copy — WebAuthn fields want BufferSource, not Uint8Array<ArrayBufferLike>. */
function bufferSource(bytes: Uint8Array): ArrayBuffer {
  const out = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(out).set(bytes);
  return out;
}

function randomChallenge(): ArrayBuffer {
  return bufferSource(crypto.getRandomValues(new Uint8Array(32)));
}

function bufferSourceFromBase64(value: string): ArrayBuffer {
  return bufferSource(base64ToBytes(value));
}

/** Strip a stored record's DEK fields back to the bare VMK wrap_meta ({v, copies, scheme?}). */
function baseVmkWrapMeta(wrapMeta: string): string {
  const parsed = JSON.parse(wrapMeta) as Record<string, unknown>;
  delete parsed.dek_nonce;
  delete parsed.wrapped_dek;
  return JSON.stringify(parsed);
}

/** Record the WebAuthn RP ID (host) a passkey was bound to, so unlock only offers it on
 *  the same site (the credential can't be asserted from a different host). */
function withRpId(wrapMeta: string, rpId: string): string {
  const meta = JSON.parse(wrapMeta) as Record<string, unknown>;
  meta.rp_id = rpId;
  return JSON.stringify(meta);
}

function toUint8(buffer: ArrayBuffer | ArrayBufferView | ArrayLike<number>): Uint8Array {
  if (buffer instanceof ArrayBuffer) return new Uint8Array(buffer);
  if (ArrayBuffer.isView(buffer)) return new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  return Uint8Array.from(buffer);
}

function copyUint8(buffer: ArrayBuffer | ArrayBufferView | ArrayLike<number>): Uint8Array {
  return new Uint8Array(toUint8(buffer));
}

export function readPasskeyPrfResult(credential: PublicKeyCredential | null): Uint8Array {
  const ext = credential?.getClientExtensionResults() as { prf?: { results?: { first?: ArrayBuffer | ArrayBufferView | ArrayLike<number> } } } | undefined;
  const first = ext?.prf?.results?.first;
  if (!first) throw new Error('passkey-prf-unavailable');
  const prfOutput = copyUint8(first);
  if (prfOutput.byteLength === 0) throw new Error('passkey-prf-unavailable');
  return prfOutput;
}

export type PasskeyEntry = { credentialId?: string; prfSalt: Uint8Array };

function singlePasskeyEntry(entries: PasskeyEntry[]): PasskeyEntry {
  if (entries.length === 0) throw new Error('passkey-not-configured');
  if (entries.length > 1) throw new Error('passkey-multiple-not-supported');
  return entries[0];
}

export function passkeyCreationOptions(
  rpId: string,
  challenge: ArrayBuffer | ArrayBufferView | ArrayLike<number> = randomChallenge(),
): PublicKeyCredentialCreationOptions {
  return {
    rp: { name: WEBAUTHN_RP_NAME, id: rpId },
    user: { id: bufferSource(WEBAUTHN_USER_HANDLE), name: 'avibe-vault', displayName: WEBAUTHN_RP_NAME },
    challenge: bufferSource(toUint8(challenge)),
    pubKeyCredParams: [
      { type: 'public-key', alg: -7 },
      { type: 'public-key', alg: -257 },
    ],
    authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
    extensions: { prf: {} } as AuthenticationExtensionsClientInputs,
  };
}

function passkeyCreationOptionsFromServer(
  webauthn: Awaited<ReturnType<ApiContextType['createVaultAuthzWebAuthnOptions']>>['webauthn'],
): PublicKeyCredentialCreationOptions {
  return {
    rp: webauthn.rp,
    user: { ...webauthn.user, id: bufferSourceFromBase64(webauthn.user.id) },
    challenge: bufferSourceFromBase64(webauthn.challenge),
    pubKeyCredParams: webauthn.pubKeyCredParams,
    authenticatorSelection: webauthn.authenticatorSelection,
    extensions: webauthn.extensions,
  };
}

export function passkeyPrfAssertionOptions(entries: PasskeyEntry[], rpId: string): PublicKeyCredentialRequestOptions {
  const entry = singlePasskeyEntry(entries);
  const options: PublicKeyCredentialRequestOptions = {
    challenge: randomChallenge(),
    rpId,
    userVerification: 'required',
    extensions: webAuthnPrfExtensionInput(entry.prfSalt) as AuthenticationExtensionsClientInputs,
  };
  if (entry.credentialId) {
    options.allowCredentials = [
      {
        type: 'public-key' as const,
        id: bufferSource(base64ToBytes(entry.credentialId)),
      },
    ];
  }
  return options;
}

export function passkeyAssertionOptionsFromServer(
  webauthn: VaultDeleteChallengeResult['webauthn'],
): PublicKeyCredentialRequestOptions {
  return {
    challenge: bufferSourceFromBase64(webauthn.challenge),
    rpId: webauthn.rpId,
    userVerification: webauthn.userVerification,
    allowCredentials: webauthn.allowCredentials.map((entry) => ({
      type: 'public-key' as const,
      id: bufferSourceFromBase64(entry.id),
      ...(entry.transports ? { transports: entry.transports } : {}),
    })),
  };
}

function serializeCredentialBase(credential: PublicKeyCredential): Pick<VaultWebAuthnSerializedCredential, 'id' | 'rawId' | 'type'> {
  return {
    id: credential.id,
    rawId: bytesToBase64(toUint8(credential.rawId)),
    type: credential.type,
  };
}

export function serializeAttestationCredential(credential: PublicKeyCredential): VaultWebAuthnSerializedCredential {
  const response = credential.response as AuthenticatorAttestationResponse;
  const transports = typeof response.getTransports === 'function' ? response.getTransports() : [];
  return {
    ...serializeCredentialBase(credential),
    response: {
      clientDataJSON: bytesToBase64(toUint8(response.clientDataJSON)),
      attestationObject: bytesToBase64(toUint8(response.attestationObject)),
      transports,
    },
  };
}

export function serializeAssertionCredential(credential: PublicKeyCredential): VaultWebAuthnSerializedCredential {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    ...serializeCredentialBase(credential),
    response: {
      clientDataJSON: bytesToBase64(toUint8(response.clientDataJSON)),
      authenticatorData: bytesToBase64(toUint8(response.authenticatorData)),
      signature: bytesToBase64(toUint8(response.signature)),
      userHandle: response.userHandle ? bytesToBase64(toUint8(response.userHandle)) : null,
    },
  };
}

/**
 * Assert the vault passkey and extract its PRF output. Keep the assertion on the
 * simple `prf.eval` path: iOS 1Password currently fails on `evalByCredential`,
 * while `allowCredentials + prf.eval` preserves the exact credential binding.
 */
async function assertPasskeyPrf(entries: PasskeyEntry[]): Promise<{ prfOutput: Uint8Array; prfSalt: Uint8Array }> {
  const assertion = (await navigator.credentials.get({
    publicKey: passkeyPrfAssertionOptions(entries, window.location.hostname),
  })) as PublicKeyCredential | null;
  if (!assertion) throw new Error('passkey-cancelled');
  const prfOutput = readPasskeyPrfResult(assertion);
  const usedId = bytesToBase64(toUint8(assertion.rawId));
  const used = entries.find((entry) => entry.credentialId === usedId);
  return { prfOutput, prfSalt: used?.prfSalt ?? entries[0].prfSalt };
}

/** Create a resident passkey, then assert it once to extract the PRF output. */
async function setupPasskeyFactor(
  api: ApiContextType,
  prfSalt: Uint8Array,
): Promise<{ prfOutput: Uint8Array; credentialId: string; registration: VaultWebAuthnRegistrationPayload }> {
  const options = await api.createVaultAuthzWebAuthnOptions();
  if (!options?.ok) throw new Error(options?.code || 'passkey-registration-options-failed');
  const created = (await navigator.credentials.create({
    publicKey: passkeyCreationOptionsFromServer(options.webauthn),
  })) as PublicKeyCredential | null;
  if (!created) throw new Error('passkey-cancelled');
  const credentialId = bytesToBase64(toUint8(created.rawId));
  const { prfOutput } = await assertPasskeyPrf([{ credentialId, prfSalt }]);
  return {
    prfOutput,
    credentialId,
    registration: {
      challenge_id: options.challenge_id,
      credential: serializeAttestationCredential(created),
    },
  };
}

export function useProtectedVault() {
  const api = useApi();
  const [status, setStatus] = useState<ProtectedVaultStatus>(sessionVault.vmk ? 'unlocked' : 'checking');
  const [error, setError] = useState<string | null>(null);

  // Re-sync this instance when the module VMK locks/unlocks elsewhere — notably when the
  // auto-lock timer fires while this component is mounted, or another instance unlocks. Don't
  // clobber the async discovery states ('checking'/'error').
  useEffect(
    () =>
      subscribeVaultLock(() => {
        setStatus((prev) => {
          // A global unlock (from another dialog/tab) should win even over a stale 'error'/'checking'
          // discovery state — otherwise that instance stays disabled though the vault is now unlocked.
          if (sessionVault.vmk) return 'unlocked';
          // No VMK: don't clobber an in-flight discovery; otherwise reflect locked/needs-setup.
          return prev === 'checking' || prev === 'error' ? prev : vaultStatusNow();
        });
      }),
    [],
  );

  const refresh = useCallback(async () => {
    enforceAutoLock(); // a cached VMK past its deadline isn't "unlocked" — lock it before trusting it
    if (sessionVault.vmk) {
      setStatus('unlocked');
      return;
    }
    setStatus('checking');
    setError(null);
    try {
      const res = await api.getVaultVmk();
      if (!res?.ok) throw new Error('vmk-discovery-failed');
      if (res.exists && res.wrap_meta) {
        sessionVault.wrapMeta = baseVmkWrapMeta(res.wrap_meta);
        setStatus('locked');
      } else {
        sessionVault.wrapMeta = null;
        setStatus('needs-setup');
      }
    } catch {
      // A failed/transient discovery must NOT degrade to setup — that would let the
      // user mint a second VMK and split the vault key history. Surface an error.
      setStatus('error');
      setError('vmk-discovery-failed');
    }
  }, [api]);

  const commit = (
    vmk: Uint8Array,
    wrapMeta: string,
    freshSetup: boolean,
    authzFactorRegistration: VaultWebAuthnRegistrationPayload | null = null,
  ) => {
    sessionVault.vmk?.fill(0);
    sessionVault.vmk = vmk;
    sessionVault.wrapMeta = wrapMeta;
    sessionVault.freshSetup = freshSetup;
    sessionVault.authzFactorRegistration = authzFactorRegistration;
    armVaultAutoLock();
    setStatus('unlocked');
    setError(null);
  };

  // First-time setup is passkey-only. A lost passkey is unrecoverable for now, so the
  // UI requires an explicit acknowledgement before calling this.
  const setupPasskey = useCallback(async () => {
    const prfSalt = newPasskeyPrfSalt();
    const { prfOutput, credentialId, registration } = await setupPasskeyFactor(api, prfSalt);
    const vmk = newVmk();
    const wrapMeta = await buildWrapMeta(vmk, [{ kind: 'passkey', prfOutput, prfSalt, credentialId }]);
    commit(vmk, withRpId(wrapMeta, window.location.hostname), true, registration);
  }, [api]);

  const unlockPasskey = useCallback(async () => {
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) throw new Error('vault-not-setup');
    const entries = passkeyPrfSaltEntries(wrapMeta);
    if (entries.length === 0) throw new Error('passkey-not-configured');
    const { prfOutput, prfSalt } = await assertPasskeyPrf(entries);
    commit(await unwrapVmk(wrapMeta, { kind: 'passkey', prfOutput, prfSalt }), wrapMeta, false);
  }, []);

  /**
   * Seal a value under the unlocked VMK into a stored protected envelope. Accepts a
   * string (static secret) or raw bytes (a keypair's 32-byte private key), so protected
   * signing keys seal the key material itself, not a UTF-8 string.
   */
  const sealValue = useCallback(
    async (
      name: string,
      value: Uint8Array | string,
    ): Promise<{
      envelope: ProtectedRecordEnvelope;
      establishingVmk: boolean;
      authzFactorRegistration?: VaultWebAuthnRegistrationPayload;
    }> => {
      if (!enforceAutoLock()) throw new Error('vault-locked');
      const { vmk, wrapMeta, freshSetup } = sessionVault;
      if (!vmk || !wrapMeta) throw new Error('vault-locked');
      armVaultAutoLock();
      const valueBytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
      const sealed = await sealProtected(valueBytes, vmk, { name });
      // `establishingVmk` lets the create transaction enforce the atomic single-init
      // guard server-side (a UI re-check here can't be race-free); the daemon rejects a
      // second VMK so concurrent first-time setups can't split the key history.
      return {
        envelope: packProtectedRecord(sealed, wrapMeta),
        establishingVmk: freshSetup,
        authzFactorRegistration: freshSetup ? (sessionVault.authzFactorRegistration ?? undefined) : undefined,
      };
    },
    [],
  );

  const afterCreated = useCallback(() => {
    // The vault now exists server-side; subsequent creates this session aren't "establishing".
    sessionVault.freshSetup = false;
    sessionVault.authzFactorRegistration = null;
  }, []);

  /**
   * Sign a digest locally with a protected keypair, approving a per-use sign request.
   * The private key is opened from the request's unlock material under the cached VMK,
   * used, and zeroed inside {@link signProtectedDigest} — only the public signature
   * leaves the browser. The VMK never escapes this module.
   */
  const signProtectedRequest = useCallback(
    async (material: ProtectedUnlockMaterial, digest: string, scheme: SignatureScheme): Promise<SignatureResult> => {
      if (!enforceAutoLock()) throw new Error('vault-locked');
      const { vmk } = sessionVault;
      if (!vmk) throw new Error('vault-locked');
      armVaultAutoLock();
      const { sealed } = unpackProtectedRecord(material.envelope);
      return signProtectedDigest(sealed, vmk, { name: material.name }, digest, scheme);
    },
    [],
  );

  /**
   * Release a protected secret's DEK as an opaque HPKE blind box addressed to the
   * resident avault agent, approving a protected value-access request. The released DEK
   * is sealed to the agent's pinned public key inside {@link releaseProtectedDek}; the
   * daemon only ever relays the resulting blind box, never a raw DEK or plaintext.
   */
  const releaseProtectedDelivery = useCallback(
    async (
      material: ProtectedUnlockMaterial,
      publicKey: AvaultPublicKey,
      context: ProtectedDekDeliveryBlindBoxContext,
    ): Promise<BlindBox> => {
      if (!enforceAutoLock()) throw new Error('vault-locked');
      const { vmk } = sessionVault;
      if (!vmk) throw new Error('vault-locked');
      armVaultAutoLock();
      const { sealed } = unpackProtectedRecord(material.envelope);
      return releaseProtectedDek(sealed, vmk, publicKey, { name: material.name }, context);
    },
    [],
  );

  const authorizeProtectedDelete = useCallback(
    async (name: string): Promise<VaultDeleteAuthz> => {
      const challenge = await api.createVaultDeleteChallenge(name, { handleError: false });
      if (!challenge?.ok) {
        throw new Error(challenge?.code || challenge?.message || 'protected-delete-challenge-failed');
      }
      const assertion = (await navigator.credentials.get({
        publicKey: passkeyAssertionOptionsFromServer(challenge.webauthn),
      })) as PublicKeyCredential | null;
      if (!assertion) throw new Error('passkey-cancelled');
      const rawId = bytesToBase64(toUint8(assertion.rawId));
      const factor = challenge.webauthn.allowCredentials.find((entry) => entry.id === rawId);
      if (!factor?.factor_id) throw new Error('protected-authz-factor-missing');
      return {
        kind: 'webauthn',
        challenge_id: challenge.challenge_id,
        factor_id: factor.factor_id,
        assertion: serializeAssertionCredential(assertion),
      };
    },
    [api],
  );

  const lock = useCallback(() => {
    lockVault(true);
    setStatus(vaultStatusNow());
  }, []);

  const discardAndRefresh = useCallback(async () => {
    // After an init collision (another tab established the vault first), drop the
    // rejected local VMK and reload the server's real wrap_meta so the user unlocks the
    // established vault instead of resealing under the loser VMK.
    clearVmk();
    sessionVault.wrapMeta = null;
    sessionVault.freshSetup = false;
    sessionVault.authzFactorRegistration = null;
    notifyVaultLockChange();
    await refresh();
  }, [refresh]);

  const hasPasskey = useCallback(() => {
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) return false;
    try {
      return passkeyPrfSaltEntries(wrapMeta).length > 0;
    } catch {
      return false;
    }
  }, []);

  // A passkey can only be asserted on the host (RP ID) it was created on. Offer passkey
  // unlock only when the current host matches the stored one (legacy vaults without a
  // recorded rp_id are not blocked).
  const passkeyUsableHere = useCallback(() => {
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) return false;
    try {
      const meta = JSON.parse(wrapMeta) as { rp_id?: string };
      return !meta.rp_id || meta.rp_id === window.location.hostname;
    } catch {
      return true;
    }
  }, []);

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
    authorizeProtectedDelete,
    afterCreated,
    lock,
    discardAndRefresh,
    hasPasskey,
    passkeyUsableHere,
  };
}

/**
 * Reactive VMK lock state for a lightweight status/countdown UI. Subscribes to the
 * module-global auto-lock so an indicator can show the time left before the vault
 * auto-locks and offer an immediate "Lock now", without taking the full
 * {@link useProtectedVault} surface.
 */
export function useVaultLock(): { unlocked: boolean; remainingMs: number; lockNow: () => void } {
  const [, forceRender] = useReducer((n: number) => n + 1, 0);
  useEffect(() => subscribeVaultLock(forceRender), []);

  const unlocked = vaultUnlocked();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!unlocked) return;
    setNow(Date.now());
    // Tick the countdown, and enforce the deadline by wall-clock so a delayed timer (e.g. the tab
    // was suspended) locks promptly on resume instead of lingering unlocked.
    const id = setInterval(() => {
      enforceAutoLock();
      setNow(Date.now());
    }, 1000);
    return () => clearInterval(id);
  }, [unlocked]);

  const expiresAt = vaultUnlockExpiresAt();
  const remainingMs = expiresAt ? Math.max(0, expiresAt - now) : 0;
  // "Lock now" broadcasts so every open tab for this origin wipes its VMK, not just this one.
  return { unlocked, remainingMs, lockNow: () => lockVault(true) };
}
