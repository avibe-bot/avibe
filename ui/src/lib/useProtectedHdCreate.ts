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
import { blockHdWallet, isHdWalletBlocked, unblockHdWallet } from './vaultHdQuarantine';
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

/** Avibe's answer to a refused save: its code (shown in the UI's language when known) and its text. */
export type ProtectedHdRefusal = { code: string | null; text: string };

export type ProtectedHdPhase =
  | { kind: 'idle' }
  | { kind: 'waiting' }
  | { kind: 'failed'; reason: ProtectedHdLaunchFailure }
  | { kind: 'saving' }
  | { kind: 'checking'; refusal: ProtectedHdRefusal | null }
  | { kind: 'unconfirmed'; refusal: ProtectedHdRefusal | null }
  | { kind: 'mismatch' }
  /** Refused because another key has the name: this wallet was not saved (the sealed result is bound
   *  to the name, so it can't be saved under another one). The name is editable again. */
  | { kind: 'nameTaken'; mode: 'create' | 'import' }
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
  /** Whether the name is still free, asked with the window reserved but before anything is made in it. */
  precheck?: () => Promise<boolean>;
  /** Called when the save was refused because another key has the name. */
  onNameTaken?: () => void;
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

// Refusals the daemon gives before writing anything (contract rev0.7: definite received refusals).
// `vault_already_initialized` is ambiguous about establishment and is not one of them.
const definiteRefusal = (result: { ok?: unknown; code?: unknown } | null | undefined): boolean =>
  result?.ok === false && typeof result.code === 'string'
  && (result.code === 'secret_exists' || result.code === 'secret_name_case_conflict' || /^invalid_[a-z_]+$/.test(result.code));

const refusalOf = (result: { code?: string; message?: string } | null | undefined): ProtectedHdRefusal | null => {
  const code = result?.code?.trim() || null;
  const text = result?.message?.trim() || code;
  return text ? { code, text } : null;
};

/** Whether two #0 address sets are the same wallet's (every listed chain's address equal). */
const sameAccount0 = (a: Record<string, string>, b: Record<string, string>) => {
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((key) => a[key] === b[key]);
};

export function useProtectedHdCreate() {
  const api = useApi();
  const [phase, setPhase] = useState<ProtectedHdPhase>({ kind: 'idle' });
  // The completed operation stays here, in memory, until the save is confirmed or the dialog closes.
  const operation = useRef<ProtectedHdOperation | null>(null);
  const knownId = useRef<string | null>(null);
  // Whether this operation keeps its wallet blocked: until an exact match, or a definite refusal.
  const holdsBlock = useRef(false);
  const pendingSince = useRef(0);
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
    unblockHdWallet(op.name);
    setPhase({ kind: 'done', name: op.name, account0: op.expectedAccount0 });
    onSavedRef.current();
    return 'match';
  }, []);

  /** Ask the daemon for the wallet by its exact name and compare it with this operation. */
  const check = useCallback(async (refusal: ProtectedHdRefusal | null) => {
    const op = operation.current;
    if (!op) return;
    setPhase({ kind: 'checking', refusal });
    try {
      const result = await api.getVaultHdReconciliation(op.name);
      if (result?.ok && confirmWith(result.reconciliation) === 'match') return;
    } catch {
      // Unreachable, timed out or not JSON: still unconfirmed.
    }
    // Not confirmed: the save's outcome is still unknown (a dropped answer settles nothing on the
    // server), so the block stays pending, dated from when the save started: list pruning keeps it
    // until an exact match or a definite refusal, and only ages it after the longest a save can take.
    if (holdsBlock.current) blockHdWallet(op.name, pendingSince.current, true);
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
    holdsBlock.current = false;
    onSavedRef.current = input.onSaved;
    openWindow.current = authorization;
    setPhase({ kind: 'waiting' });
    return (async () => {
      if (input.precheck && !(await input.precheck())) {
        // The name is taken: nothing is made; the form says why.
        authorization.close();
        openWindow.current = null;
        if (alive.current) setPhase({ kind: 'idle' });
        return;
      }
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
      // Blocked from funding in this browser (no addresses, no accounts, approvals refused) from
      // before the request leaves until an exact match, so closing the dialog while it is pending
      // never shows a wallet that is not confirmed. A definite refusal lifts only this block.
      const blockedBefore = isHdWalletBlocked(input.name);
      pendingSince.current = Date.now();
      blockHdWallet(input.name, pendingSince.current, true);
      holdsBlock.current = true;
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
        // An id the save answer named is observed even when the rest of its snapshot is unreadable:
        // a later read must then be that same record (rev0.7: only an answer lost before any id was
        // seen may learn one, and only after a complete match). Nothing else in it is trusted.
        const seenId = (created.reconciliation as { secret_id?: unknown } | null | undefined)?.secret_id;
        if (typeof seenId === 'string' && seenId) knownId.current = seenId;
        const outcome = confirmWith(created.reconciliation);
        if (outcome === 'match') return;
        if (outcome === 'mismatch') {
          // The saved record is not the wallet the window made: blocked here and on the list, never deleted.
          blockHdWallet(input.name);
          setPhase({ kind: 'mismatch' });
          return;
        }
        await check(null);
        return;
      }
      // A definite refusal: this request created nothing, and the name is another record's (not ours
      // to block). Anything else, an empty or unreadable answer included, stays blocked while checked.
      if (definiteRefusal(created)) {
        holdsBlock.current = false;
        if (blockedBefore) blockHdWallet(input.name);
        else unblockHdWallet(input.name);
      }
      if (created && created.ok === false && (created.code === 'secret_exists' || created.code === 'secret_name_case_conflict')) {
        // This request inserted nothing (a definite refusal), and an operation sends one save. New
        // words were never saved before, so for a created wallet the record under this name is
        // another key's. Imported words may have been saved by an earlier attempt: unless the record
        // there is readable and is a different wallet, say what is known (unconfirmed), not "not saved".
        setPhase({ kind: 'checking', refusal: refusalOf(created) });
        let other = input.mode === 'create';
        try {
          const result = await api.getVaultHdReconciliation(input.name);
          if (result?.ok && confirmWith(result.reconciliation) === 'match') return;
          const parsed = result?.ok ? parseProtectedHdReconciliation(result.reconciliation) : null;
          // Another seed's wallet, or a record that is not a protected seed wallet at all.
          if (parsed && !sameAccount0(parsed.expected_account0, sealed.expectedAccount0)) other = true;
          if (result?.ok === false && result.code === 'invalid_hd_wallet') other = true;
        } catch {
          // Unreadable: decided by the mode alone.
        }
        if (other) {
          operation.current = null;
          if (alive.current) setPhase({ kind: 'nameTaken', mode: input.mode });
          input.onNameTaken?.();
          return;
        }
      }
      await check(created ? refusalOf(created) : null);
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
