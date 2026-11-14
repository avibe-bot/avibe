// Creating or importing a protected seed-phrase wallet from the Add key dialog (contract rev0.7,
// ui/design: vault/mnemonic/protected/*). The click opens the sandbox window; everything secret
// happens there. This page then saves the opaque result and confirms, by an exact comparison with
// what the daemon reports, that the saved wallet is the one that window made.
//
// Before anything is sent to Avibe, a failure means nothing was saved. Once saving has started, no
// failure is ever reported as "not saved": a lost or refused answer leaves the wallet unconfirmed
// and blocked from funding until a complete match, and nothing is retried, re-created or deleted.
import { useCallback, useContext, useEffect, useRef, useState } from 'react';

import { useApi, type VaultCreatePayload, type VaultWebAuthnRegistrationPayload } from '@/context/ApiContext';

import { ProtectedHdDesignState } from './protectedHdDesign';
import { openVaultAuthorizationWindow, VaultSandboxError } from './vaultSandboxClient';
import {
  parseProtectedHdReconciliation,
  protectedHdMatches,
  protectedHdOperation,
  type ProtectedHdAccount0,
  type ProtectedHdOperation,
  type ProtectedHdSealResult,
} from './vaultProtectedHd';

/** Why the window ended without a result; in every case nothing reached Avibe. */
export type ProtectedHdLaunchFailure = 'blocked' | 'cancelled' | 'closed' | 'timeout' | 'reset' | 'unavailable' | 'failed';

export type ProtectedHdPhase =
  | { kind: 'idle' }
  | { kind: 'waiting' }
  | { kind: 'failed'; reason: ProtectedHdLaunchFailure }
  | { kind: 'saving' }
  | { kind: 'checking'; refusal: string | null }
  | { kind: 'unconfirmed'; refusal: string | null }
  | { kind: 'mismatch' }
  | { kind: 'done'; name: string; account0: ProtectedHdAccount0 };

/** Whether the page must stay as it is: the window is open, or a save may already be in Avibe. */
export const protectedHdBusy = (phase: ProtectedHdPhase) =>
  phase.kind === 'waiting' || phase.kind === 'saving' || phase.kind === 'checking';

/** After saving started the wallet may exist, so the form can't be edited or submitted again. */
export const protectedHdSubmitted = (phase: ProtectedHdPhase) =>
  phase.kind === 'saving' || phase.kind === 'checking' || phase.kind === 'unconfirmed' || phase.kind === 'mismatch' || phase.kind === 'done';
type Sealed = ProtectedHdSealResult & { authzFactorRegistration?: VaultWebAuthnRegistrationPayload };

export type ProtectedHdStart = {
  name: string;
  mode: 'create' | 'import';
  /** The wallet's non-secret metadata, sent with the opaque result. */
  metadata: Pick<VaultCreatePayload, 'description' | 'tags' | 'links' | 'provision_request_id'>;
  seal: (name: string, mode: 'create' | 'import', authorizationWindow: string) => Promise<Sealed>;
  onSaved: () => void;
};

const LAUNCH_FAILURES: Record<string, ProtectedHdLaunchFailure> = {
  authorization_popup_blocked: 'blocked',
  authorization_cancelled: 'cancelled',
  authorization_window_closed: 'closed',
  authorization_window_timeout: 'timeout',
  sandbox_request_timeout: 'timeout',
  sandbox_reset: 'reset',
  protected_hd_unavailable: 'unavailable',
};

function launchFailure(error: unknown): ProtectedHdLaunchFailure {
  if (error instanceof VaultSandboxError) {
    if (LAUNCH_FAILURES[error.code]) return LAUNCH_FAILURES[error.code];
    if (/vault-operation-aborted/.test(error.message)) return 'reset';
  }
  return 'failed';
}

const refusalText = (result: { code?: string; message?: string } | null | undefined): string | null =>
  result?.message?.trim() || result?.code?.trim() || null;

export function useProtectedHdCreate() {
  const api = useApi();
  const [phase, setPhase] = useState<ProtectedHdPhase>({ kind: 'idle' });
  // The completed operation stays here, in memory, until the save is confirmed or the dialog closes.
  const operation = useRef<ProtectedHdOperation | null>(null);
  const knownId = useRef<string | null>(null);
  const onSavedRef = useRef<() => void>(() => undefined);
  // Closing the dialog while the window is open closes the window too, and a result that arrives
  // after that is dropped before anything is sent: nothing is saved behind a closed dialog.
  const alive = useRef(true);
  const openWindow = useRef<{ close: () => void } | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      openWindow.current?.close();
    };
  }, []);

  const confirmWith = useCallback((snapshot: unknown): 'match' | 'mismatch' | 'unknown' => {
    const op = operation.current;
    const parsed = parseProtectedHdReconciliation(snapshot);
    if (!op || !parsed) return 'unknown';
    if (!protectedHdMatches(parsed, op, knownId.current)) return 'mismatch';
    knownId.current = parsed.secret_id;
    setPhase({ kind: 'done', name: op.name, account0: op.expectedAccount0 });
    onSavedRef.current();
    return 'match';
  }, []);

  /** Ask the daemon for the wallet by its exact name and compare it with this operation. */
  const check = useCallback(async (refusal: string | null) => {
    const op = operation.current;
    if (!op) return;
    setPhase({ kind: 'checking', refusal });
    try {
      const result = await api.getVaultHdReconciliation(op.name);
      if (result?.ok && confirmWith(result.reconciliation) === 'match') return;
    } catch {
      // Unreachable, timed out or not JSON: still unconfirmed.
    }
    setPhase({ kind: 'unconfirmed', refusal });
  }, [api, confirmWith]);

  /** Must be called from the click itself: the window is opened before anything is awaited. */
  const start = useCallback((input: ProtectedHdStart) => {
    const authorization = openVaultAuthorizationWindow();
    if (!authorization) {
      setPhase({ kind: 'failed', reason: 'blocked' });
      return Promise.resolve();
    }
    operation.current = null;
    knownId.current = null;
    onSavedRef.current = input.onSaved;
    openWindow.current = authorization;
    setPhase({ kind: 'waiting' });
    return (async () => {
      let sealed: Sealed;
      try {
        sealed = await input.seal(input.name, input.mode, authorization.id);
      } catch (error) {
        setPhase({ kind: 'failed', reason: launchFailure(error) });
        return;
      } finally {
        authorization.close();
        openWindow.current = null;
      }
      if (!alive.current) return;
      // From here on Avibe may have saved it: never "not saved", never a retry or a new seed.
      operation.current = protectedHdOperation(input.name, sealed);
      setPhase({ kind: 'saving' });
      const payload: VaultCreatePayload = {
        name: input.name,
        kind: 'keypair',
        signer_kind: 'hd-seed',
        protection: 'protected',
        sealed: sealed.envelope,
        accounts: sealed.accounts,
        expected_account0: sealed.expectedAccount0,
        ...input.metadata,
        ...(sealed.establishingVmk ? { establishing_vmk: true } : {}),
        ...(sealed.authzFactorRegistration ? { authz_factor_registration: sealed.authzFactorRegistration } : {}),
      };
      // A thrown request (dropped connection, timeout) is a lost answer: null, then checked by name.
      const created = await api.createVaultSecret(payload, { handleError: false }).catch(() => null);
      if (created?.ok) {
        const parsed = parseProtectedHdReconciliation(created.reconciliation);
        if (parsed) knownId.current = parsed.secret_id;
        const outcome = confirmWith(created.reconciliation);
        if (outcome === 'match') return;
        if (outcome === 'mismatch') {
          setPhase({ kind: 'mismatch' });
          return;
        }
        await check(null);
        return;
      }
      await check(created ? refusalText(created) : null);
    })();
  }, [api, check, confirmWith]);

  const checkAgain = useCallback(() => {
    const refusal = phase.kind === 'unconfirmed' ? phase.refusal : null;
    void check(refusal);
  }, [check, phase]);

  const reset = useCallback(() => {
    if (phase.kind === 'failed' || phase.kind === 'idle') setPhase({ kind: 'idle' });
  }, [phase.kind]);

  const designed = useContext(ProtectedHdDesignState);
  if (designed) return { phase: designed.phase, start: () => Promise.resolve(), checkAgain: () => undefined, reset: () => undefined };
  return { phase, start, checkAgain, reset };
}
