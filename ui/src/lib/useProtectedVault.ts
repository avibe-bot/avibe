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
  sealProtected,
  unwrapVmk,
  webAuthnPrfExtensionInput,
  type ProtectedRecordEnvelope,
} from './vaultCrypto';

/**
 * Protected-tier vault lifecycle for the Web UI.
 *
 * The Vault Master Key (VMK) lives only in browser memory and is wrapped by user
 * factors (passkey-PRF first, password as a "less secure" fallback) into an opaque
 * `wrap_meta` the daemon stores per protected secret. This hook discovers whether the
 * vault is set up (`GET /api/vault/vmk`), runs the passkey/password setup or unlock
 * ceremony, and seals new protected values for `createVaultSecret`. The daemon never
 * sees the VMK or plaintext. No cross-origin sandbox yet — that hardening is a later
 * version.
 *
 * The unlocked VMK is cached at module scope (not per hook instance) so it survives
 * `VaultSecretForm` unmount/remount within one page session — the user unlocks once and
 * can add several protected secrets. A full reload re-initialises the module (VMK gone)
 * and `lock()` clears it explicitly.
 */
export type ProtectedVaultStatus = 'checking' | 'needs-setup' | 'locked' | 'unlocked' | 'error';

const sessionVault: { vmk: Uint8Array | null; wrapMeta: string | null } = { vmk: null, wrapMeta: null };

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
 * returns the salt of whichever credential actually responded — so unlock works no
 * matter which passkey the device holds (not just the first copy).
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
      // Only a clean exists:false response means "no vault yet". A failed/transient
      // discovery must NOT degrade to setup — that would let the user mint a second
      // VMK and split the vault key history. Surface an error and let them retry.
      setStatus('error');
      setError('vmk-discovery-failed');
    }
  }, [api]);

  const commit = (vmk: Uint8Array, wrapMeta: string) => {
    sessionVault.vmk?.fill(0);
    sessionVault.vmk = vmk;
    sessionVault.wrapMeta = wrapMeta;
    setStatus('unlocked');
    setError(null);
  };

  const setupPassword = useCallback(async (password: string) => {
    const vmk = newVmk();
    commit(vmk, await buildWrapMeta(vmk, [{ kind: 'password', password }]));
  }, []);

  const setupPasskey = useCallback(async () => {
    const prfSalt = newPasskeyPrfSalt();
    const { prfOutput, credentialId } = await setupPasskeyFactor(prfSalt);
    const vmk = newVmk();
    commit(vmk, await buildWrapMeta(vmk, [{ kind: 'passkey', prfOutput, prfSalt, credentialId }]));
  }, []);

  const unlockPassword = useCallback(async (password: string) => {
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) throw new Error('vault-not-setup');
    commit(await unwrapVmk(wrapMeta, { kind: 'password', password }), wrapMeta);
  }, []);

  const unlockPasskey = useCallback(async () => {
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) throw new Error('vault-not-setup');
    const entries = passkeyPrfSaltEntries(wrapMeta);
    if (entries.length === 0) throw new Error('passkey-not-configured');
    const { prfOutput, prfSalt } = await assertPasskeyPrf(entries);
    commit(await unwrapVmk(wrapMeta, { kind: 'passkey', prfOutput, prfSalt }), wrapMeta);
  }, []);

  /** Seal a value under the unlocked VMK into a stored protected envelope. */
  const sealValue = useCallback((name: string, value: string): Promise<ProtectedRecordEnvelope> => {
    const { vmk, wrapMeta } = sessionVault;
    if (!vmk || !wrapMeta) throw new Error('vault-locked');
    return sealProtected(new TextEncoder().encode(value), vmk, { name }).then((sealed) => packProtectedRecord(sealed, wrapMeta));
  }, []);

  const lock = useCallback(() => {
    sessionVault.vmk?.fill(0);
    sessionVault.vmk = null;
    setStatus(sessionVault.wrapMeta ? 'locked' : 'needs-setup');
  }, []);

  const hasPasskey = useCallback(() => {
    const wrapMeta = sessionVault.wrapMeta;
    if (!wrapMeta) return false;
    try {
      return passkeyPrfSaltEntries(wrapMeta).length > 0;
    } catch {
      return false;
    }
  }, []);

  return { status, error, setError, refresh, setupPassword, setupPasskey, unlockPassword, unlockPasskey, sealValue, lock, hasPasskey };
}
