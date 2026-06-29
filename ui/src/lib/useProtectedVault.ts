import { useCallback, useState } from 'react';

import { useApi } from '@/context/ApiContext';
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
 * The Vault Master Key (VMK) lives only in browser memory and is wrapped by user
 * factors into an opaque `wrap_meta` the daemon stores per protected secret. A
 * **password is always set as the recovery root**; a passkey (WebAuthn-PRF, Touch ID /
 * Windows Hello) can be added on top as the quick primary unlock — so losing a device
 * never makes protected secrets unrecoverable. The daemon never sees the VMK or
 * plaintext. No cross-origin sandbox yet — that hardening is a later version.
 *
 * The unlocked VMK is cached at module scope so it survives `VaultSecretForm`
 * unmount/remount within one page session. A full reload re-initialises the module.
 */
export type ProtectedVaultStatus = 'checking' | 'needs-setup' | 'locked' | 'unlocked' | 'error';

const sessionVault: { vmk: Uint8Array | null; wrapMeta: string | null; freshSetup: boolean } = {
  vmk: null,
  wrapMeta: null,
  freshSetup: false,
};

const WEBAUTHN_RP_NAME = 'Avibe Vault';
const WEBAUTHN_USER_HANDLE = new TextEncoder().encode('avibe-vault');

/**
 * WebAuthn needs a secure context and a domain RP ID. Browsers reject raw IP RP IDs
 * (the default local `http://127.0.0.1:5123` workflow), so the passkey path is only
 * offered on `localhost` or an HTTPS domain (e.g. the tunnel); elsewhere we fall back
 * to the password, which is the recovery root anyway.
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

/** WebAuthn `prf.evalByCredential` keys are base64url-encoded credential ids. */
function base64ToBase64Url(b64: string): string {
  return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Strip a stored record's DEK fields back to the bare VMK wrap_meta ({v, copies}). */
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

function toUint8(buffer: ArrayBuffer | ArrayBufferView): Uint8Array {
  return buffer instanceof ArrayBuffer ? new Uint8Array(buffer) : new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
}

function passkeyResult(credential: PublicKeyCredential | null): Uint8Array {
  const ext = credential?.getClientExtensionResults() as { prf?: { results?: { first?: ArrayBuffer } } } | undefined;
  const first = ext?.prf?.results?.first;
  if (!first) throw new Error('passkey-prf-unavailable');
  return toUint8(first);
}

type PasskeyEntry = { credentialId?: string; prfSalt: Uint8Array };

/**
 * Assert one of the vault's passkeys and extract its PRF output. Uses
 * `evalByCredential` so each credential is evaluated with its own stored salt, then
 * returns the salt of whichever credential actually responded.
 */
async function assertPasskeyPrf(entries: PasskeyEntry[]): Promise<{ prfOutput: Uint8Array; prfSalt: Uint8Array }> {
  const withId = entries.filter((entry) => entry.credentialId);
  const evalByCredential: Record<string, { first: ArrayBuffer }> = {};
  for (const entry of withId) {
    evalByCredential[base64ToBase64Url(entry.credentialId as string)] = { first: bufferSource(entry.prfSalt) };
  }
  const extensions = (withId.length > 0
    ? { prf: { evalByCredential } }
    : webAuthnPrfExtensionInput(entries[0].prfSalt)) as AuthenticationExtensionsClientInputs;
  const assertion = (await navigator.credentials.get({
    publicKey: {
      challenge: randomChallenge(),
      allowCredentials: withId.map((entry) => ({
        type: 'public-key' as const,
        id: bufferSource(base64ToBytes(entry.credentialId as string)),
      })),
      userVerification: 'required',
      extensions,
    },
  })) as PublicKeyCredential | null;
  if (!assertion) throw new Error('passkey-cancelled');
  const prfOutput = passkeyResult(assertion);
  const usedId = bytesToBase64(toUint8(assertion.rawId));
  const used = entries.find((entry) => entry.credentialId === usedId);
  return { prfOutput, prfSalt: used?.prfSalt ?? entries[0].prfSalt };
}

/** Create a resident passkey, then assert it once to extract the PRF output. */
async function setupPasskeyFactor(prfSalt: Uint8Array): Promise<{ prfOutput: Uint8Array; credentialId: string }> {
  const created = (await navigator.credentials.create({
    publicKey: {
      rp: { name: WEBAUTHN_RP_NAME, id: window.location.hostname },
      user: { id: bufferSource(WEBAUTHN_USER_HANDLE), name: 'avibe-vault', displayName: WEBAUTHN_RP_NAME },
      challenge: randomChallenge(),
      pubKeyCredParams: [
        { type: 'public-key', alg: -7 },
        { type: 'public-key', alg: -257 },
      ],
      authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' },
      extensions: { prf: {} } as AuthenticationExtensionsClientInputs,
    },
  })) as PublicKeyCredential | null;
  if (!created) throw new Error('passkey-cancelled');
  const credentialId = bytesToBase64(toUint8(created.rawId));
  const { prfOutput } = await assertPasskeyPrf([{ credentialId, prfSalt }]);
  return { prfOutput, credentialId };
}

export function useProtectedVault() {
  const api = useApi();
  const [status, setStatus] = useState<ProtectedVaultStatus>(sessionVault.vmk ? 'unlocked' : 'checking');
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
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

  const commit = (vmk: Uint8Array, wrapMeta: string, freshSetup: boolean) => {
    sessionVault.vmk?.fill(0);
    sessionVault.vmk = vmk;
    sessionVault.wrapMeta = wrapMeta;
    sessionVault.freshSetup = freshSetup;
    setStatus('unlocked');
    setError(null);
  };

  // First-time setup uses ONE factor. Passkey-only is the most secure option (no
  // phishable/leakable password) but is unrecoverable if the device/passkey is lost —
  // the UI requires an explicit acknowledgement. Password is the recoverable alternative.
  const setupPassword = useCallback(async (password: string) => {
    const vmk = newVmk();
    commit(vmk, await buildWrapMeta(vmk, [{ kind: 'password', password }]), true);
  }, []);

  const setupPasskey = useCallback(async () => {
    const prfSalt = newPasskeyPrfSalt();
    const { prfOutput, credentialId } = await setupPasskeyFactor(prfSalt);
    const vmk = newVmk();
    const wrapMeta = await buildWrapMeta(vmk, [{ kind: 'passkey', prfOutput, prfSalt, credentialId }]);
    commit(vmk, withRpId(wrapMeta, window.location.hostname), true);
  }, []);

  const unlockPassword = useCallback(async (password: string) => {
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) throw new Error('vault-not-setup');
    commit(await unwrapVmk(wrapMeta, { kind: 'password', password }), wrapMeta, false);
  }, []);

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
    async (name: string, value: Uint8Array | string): Promise<{ envelope: ProtectedRecordEnvelope; establishingVmk: boolean }> => {
      const { vmk, wrapMeta, freshSetup } = sessionVault;
      if (!vmk || !wrapMeta) throw new Error('vault-locked');
      const valueBytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
      const sealed = await sealProtected(valueBytes, vmk, { name });
      // `establishingVmk` lets the create transaction enforce the atomic single-init
      // guard server-side (a UI re-check here can't be race-free); the daemon rejects a
      // second VMK so concurrent first-time setups can't split the key history.
      return { envelope: packProtectedRecord(sealed, wrapMeta), establishingVmk: freshSetup };
    },
    [],
  );

  const afterCreated = useCallback(() => {
    // The vault now exists server-side; subsequent creates this session aren't "establishing".
    sessionVault.freshSetup = false;
  }, []);

  /**
   * Sign a digest locally with a protected keypair, approving a per-use sign request.
   * The private key is opened from the request's unlock material under the cached VMK,
   * used, and zeroed inside {@link signProtectedDigest} — only the public signature
   * leaves the browser. The VMK never escapes this module.
   */
  const signProtectedRequest = useCallback(
    async (material: ProtectedUnlockMaterial, digest: string, scheme: SignatureScheme): Promise<SignatureResult> => {
      const { vmk } = sessionVault;
      if (!vmk) throw new Error('vault-locked');
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
      const { vmk } = sessionVault;
      if (!vmk) throw new Error('vault-locked');
      const { sealed } = unpackProtectedRecord(material.envelope);
      return releaseProtectedDek(sealed, vmk, publicKey, { name: material.name }, context);
    },
    [],
  );

  const lock = useCallback(() => {
    sessionVault.vmk?.fill(0);
    sessionVault.vmk = null;
    if (sessionVault.freshSetup) {
      // An uncommitted fresh VMK (set up but no protected secret saved yet) — discard it
      // so a later unlock can't seal under a stale local VMK that skips the init guard.
      sessionVault.wrapMeta = null;
      sessionVault.freshSetup = false;
      setStatus('needs-setup');
    } else {
      setStatus(sessionVault.wrapMeta ? 'locked' : 'needs-setup');
    }
  }, []);

  const discardAndRefresh = useCallback(async () => {
    // After an init collision (another tab established the vault first), drop the
    // rejected local VMK and reload the server's real wrap_meta so the user unlocks the
    // established vault instead of resealing under the loser VMK.
    sessionVault.vmk?.fill(0);
    sessionVault.vmk = null;
    sessionVault.wrapMeta = null;
    sessionVault.freshSetup = false;
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

  const hasPassword = useCallback(() => {
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) return false;
    try {
      const meta = JSON.parse(wrapMeta) as { copies?: Array<{ kind?: string }> };
      return Array.isArray(meta.copies) && meta.copies.some((copy) => copy.kind === 'password');
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

  return { status, error, setError, refresh, setupPassword, setupPasskey, unlockPassword, unlockPasskey, sealValue, signProtectedRequest, releaseProtectedDelivery, afterCreated, lock, discardAndRefresh, hasPasskey, hasPassword, passkeyUsableHere };
}
