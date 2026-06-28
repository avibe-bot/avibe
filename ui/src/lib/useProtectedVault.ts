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
  type VmkWrapFactor,
} from './vaultCrypto';

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

  // First-time setup always includes a password (recovery root); a passkey is layered
  // on top when available so device loss never strands protected secrets.
  const setupPassword = useCallback(async (password: string) => {
    const vmk = newVmk();
    commit(vmk, await buildWrapMeta(vmk, [{ kind: 'password', password }]), true);
  }, []);

  const setupPasskey = useCallback(async (recoveryPassword: string) => {
    const prfSalt = newPasskeyPrfSalt();
    const { prfOutput, credentialId } = await setupPasskeyFactor(prfSalt);
    const vmk = newVmk();
    const factors: VmkWrapFactor[] = [
      { kind: 'password', password: recoveryPassword },
      { kind: 'passkey', prfOutput, prfSalt, credentialId },
    ];
    commit(vmk, await buildWrapMeta(vmk, factors), true);
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

  /** Seal a value under the unlocked VMK into a stored protected envelope. */
  const sealValue = useCallback(
    async (name: string, value: string): Promise<ProtectedRecordEnvelope> => {
      const { vmk, wrapMeta, freshSetup } = sessionVault;
      if (!vmk || !wrapMeta) throw new Error('vault-locked');
      if (freshSetup) {
        // Guard against a concurrent first-time setup (another tab) splitting the VMK:
        // if a vault now exists, this fresh VMK is stale — refuse and force a reload.
        try {
          const res = await api.getVaultVmk();
          if (res?.ok && res.exists) throw new Error('vault-already-initialized');
        } catch (err) {
          if (err instanceof Error && err.message === 'vault-already-initialized') throw err;
          // Discovery failure here is best-effort; don't block the create.
        }
      }
      const sealed = await sealProtected(new TextEncoder().encode(value), vmk, { name });
      const envelope = packProtectedRecord(sealed, wrapMeta);
      sessionVault.freshSetup = false;
      return envelope;
    },
    [api],
  );

  const lock = useCallback(() => {
    sessionVault.vmk?.fill(0);
    sessionVault.vmk = null;
    sessionVault.freshSetup = false;
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
