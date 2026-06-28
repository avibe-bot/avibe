import { useCallback, useRef, useState } from 'react';

import { useApi } from '@/context/ApiContext';
import {
  base64ToBytes,
  buildWrapMeta,
  bytesToBase64,
  newPasskeyPrfSalt,
  newVmk,
  packProtectedRecord,
  passkeyPrfSaltEntries,
  sealProtected,
  unwrapVmk,
  webAuthnPrfExtensionInput,
  type ProtectedRecordEnvelope,
} from './vaultCrypto';

/**
 * Protected-tier vault lifecycle for the Web UI.
 *
 * The Vault Master Key (VMK) lives only in browser memory (a ref, zeroized on lock).
 * It is wrapped by user factors (passkey-PRF first, password as a "less secure"
 * fallback) into an opaque `wrap_meta` the daemon stores per protected secret. This
 * hook: discovers whether the vault is set up (`GET /api/vault/vmk`), runs the
 * passkey/password setup or unlock ceremony, caches the unlocked VMK for the session,
 * and seals new protected values for `createVaultSecret`. The daemon never sees the
 * VMK or plaintext. No cross-origin sandbox yet — that hardening is a later version.
 */
export type ProtectedVaultStatus = 'checking' | 'needs-setup' | 'locked' | 'unlocked' | 'error';
export type ProtectedFactorKind = 'passkey' | 'password';

const WEBAUTHN_RP_NAME = 'Avibe Vault';
const WEBAUTHN_USER_HANDLE = new TextEncoder().encode('avibe-vault');

/** A fresh ArrayBuffer copy — WebAuthn fields want BufferSource, not Uint8Array<ArrayBufferLike>. */
function bufferSource(bytes: Uint8Array): ArrayBuffer {
  const out = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(out).set(bytes);
  return out;
}

function randomChallenge(): ArrayBuffer {
  return bufferSource(crypto.getRandomValues(new Uint8Array(32)));
}

/**
 * Strip a stored record's per-record DEK fields back to the bare VMK `wrap_meta`
 * ({v, copies}). `GET /api/vault/vmk` returns the latest record's wrap_meta, which
 * `packProtectedRecord` folded the DEK into; we must carry only the VMK factors
 * forward, or sealing the next record would reject an already-DEK-bearing wrap_meta.
 */
function baseVmkWrapMeta(wrapMeta: string): string {
  const parsed = JSON.parse(wrapMeta) as Record<string, unknown>;
  delete parsed.dek_nonce;
  delete parsed.wrapped_dek;
  return JSON.stringify(parsed);
}

function toUint8(buffer: ArrayBuffer | ArrayBufferView): Uint8Array {
  return buffer instanceof ArrayBuffer ? new Uint8Array(buffer) : new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
}

function passkeyResult(credential: PublicKeyCredential | null): Uint8Array {
  const ext = credential?.getClientExtensionResults() as { prf?: { results?: { first?: ArrayBuffer } } } | undefined;
  const first = ext?.prf?.results?.first;
  if (!first) {
    throw new Error('passkey-prf-unavailable');
  }
  return toUint8(first);
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
      // Enable PRF on the credential; the value is extracted via the assertion below.
      extensions: { prf: {} } as AuthenticationExtensionsClientInputs,
    },
  })) as PublicKeyCredential | null;
  if (!created) throw new Error('passkey-cancelled');
  const credentialId = bytesToBase64(toUint8(created.rawId));
  const prfOutput = await assertPasskeyPrf([credentialId], prfSalt);
  return { prfOutput, credentialId };
}

/** Assert an existing passkey to extract its PRF output for the given salt. */
async function assertPasskeyPrf(credentialIds: string[], prfSalt: Uint8Array): Promise<Uint8Array> {
  const assertion = (await navigator.credentials.get({
    publicKey: {
      challenge: randomChallenge(),
      allowCredentials: credentialIds.map((id) => ({ type: 'public-key' as const, id: bufferSource(base64ToBytes(id)) })),
      userVerification: 'required',
      extensions: webAuthnPrfExtensionInput(prfSalt) as AuthenticationExtensionsClientInputs,
    },
  })) as PublicKeyCredential | null;
  if (!assertion) throw new Error('passkey-cancelled');
  return passkeyResult(assertion);
}

export function useProtectedVault() {
  const api = useApi();
  const [status, setStatus] = useState<ProtectedVaultStatus>('checking');
  const [error, setError] = useState<string | null>(null);
  const vmkRef = useRef<Uint8Array | null>(null);
  // The VMK wrap_meta carried forward into each new record (base, without the per-record DEK).
  const wrapMetaRef = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    setStatus('checking');
    setError(null);
    try {
      const res = await api.getVaultVmk();
      if (res.exists && res.wrap_meta) {
        wrapMetaRef.current = baseVmkWrapMeta(res.wrap_meta);
        setStatus(vmkRef.current ? 'unlocked' : 'locked');
      } else {
        wrapMetaRef.current = null;
        setStatus(vmkRef.current ? 'unlocked' : 'needs-setup');
      }
    } catch {
      // A missing route (older daemon) means no protected vault exists yet → setup.
      wrapMetaRef.current = null;
      setStatus(vmkRef.current ? 'unlocked' : 'needs-setup');
    }
  }, [api]);

  const setVmk = (vmk: Uint8Array, wrapMeta: string) => {
    vmkRef.current?.fill(0);
    vmkRef.current = vmk;
    wrapMetaRef.current = wrapMeta;
    setStatus('unlocked');
    setError(null);
  };

  const setupPassword = useCallback(async (password: string) => {
    const vmk = newVmk();
    const wrapMeta = await buildWrapMeta(vmk, [{ kind: 'password', password }]);
    setVmk(vmk, wrapMeta);
  }, []);

  const setupPasskey = useCallback(async () => {
    const prfSalt = newPasskeyPrfSalt();
    const { prfOutput, credentialId } = await setupPasskeyFactor(prfSalt);
    const vmk = newVmk();
    const wrapMeta = await buildWrapMeta(vmk, [{ kind: 'passkey', prfOutput, prfSalt, credentialId }]);
    setVmk(vmk, wrapMeta);
  }, []);

  const unlockPassword = useCallback(async (password: string) => {
    const wrapMeta = wrapMetaRef.current;
    if (!wrapMeta) throw new Error('vault-not-setup');
    const vmk = await unwrapVmk(wrapMeta, { kind: 'password', password });
    setVmk(vmk, wrapMeta);
  }, []);

  const unlockPasskey = useCallback(async () => {
    const wrapMeta = wrapMetaRef.current;
    if (!wrapMeta) throw new Error('vault-not-setup');
    const entries = passkeyPrfSaltEntries(wrapMeta);
    if (entries.length === 0) throw new Error('passkey-not-configured');
    // Allow any configured passkey; use that copy's stored PRF salt.
    const credentialIds = entries.filter((e) => e.credentialId).map((e) => e.credentialId as string);
    const prfSalt = entries[0].prfSalt;
    const prfOutput = await assertPasskeyPrf(credentialIds, prfSalt);
    const vmk = await unwrapVmk(wrapMeta, { kind: 'passkey', prfOutput, prfSalt });
    setVmk(vmk, wrapMeta);
  }, []);

  /** Seal a value under the unlocked VMK into a stored protected envelope. */
  const sealValue = useCallback((name: string, value: string): Promise<ProtectedRecordEnvelope> => {
    const vmk = vmkRef.current;
    const wrapMeta = wrapMetaRef.current;
    if (!vmk || !wrapMeta) throw new Error('vault-locked');
    return sealProtected(new TextEncoder().encode(value), vmk, { name }).then((sealed) =>
      packProtectedRecord(sealed, wrapMeta),
    );
  }, []);

  const lock = useCallback(() => {
    vmkRef.current?.fill(0);
    vmkRef.current = null;
    setStatus(wrapMetaRef.current ? 'locked' : 'needs-setup');
  }, []);

  const hasPasskey = useCallback(() => {
    const wrapMeta = wrapMetaRef.current;
    if (!wrapMeta) return false;
    try {
      return passkeyPrfSaltEntries(wrapMeta).length > 0;
    } catch {
      return false;
    }
  }, []);

  return {
    status,
    error,
    setError,
    refresh,
    setupPassword,
    setupPasskey,
    unlockPassword,
    unlockPasskey,
    sealValue,
    lock,
    hasPasskey,
  };
}
