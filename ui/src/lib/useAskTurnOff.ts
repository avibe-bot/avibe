// The ask-before-signing switch's way off (contract v1.1): offered only when the pinned sandbox
// build lists the operation; every attempt is a fresh challenge, the popup opened in the click.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';

import { useApi, type VaultSecret } from '@/context/ApiContext';
import { getVaultSandboxClient, openVaultAuthorizationWindow } from './vaultSandboxClient';
import { setupVaultDurably } from './useProtectedVault';
import { POLICY_DOWNGRADE_OP, policyFailureFromSandbox, turnOffAskBeforeSigning, type PolicyTurnOffFailure } from './vaultSigningPolicy';

/** What setting up a Passkey from here led to (contract v1.1 §G). */
export type AskSetupOutcome = 'done' | 'conflict' | 'failed';

export type AskTurnOffState =
  | { phase: 'checking' }
  | { phase: 'unavailable' }
  | { phase: 'ready'; failure?: PolicyTurnOffFailure; setup?: AskSetupOutcome }
  | { phase: 'working'; settingUp?: boolean };

// The setup window closed or ran out of time, or the Passkey prompt was dismissed: back to the
// setup offer, said nothing about (not a failure).
const SETUP_ENDED_BY_USER = new Set(['setup_window_closed', 'setup_window_timeout']);
const setupEndedByUser = (error: unknown) => {
  const code = typeof error === 'object' && error !== null ? (error as { code?: unknown }).code : undefined;
  return (typeof code === 'string' && SETUP_ENDED_BY_USER.has(code)) || policyFailureFromSandbox(error) === 'cancelled';
};

/** Whether the line under the switch (AskTurnOffNote) has anything to say for this state. */
export const askTurnOffNoteShown = (state: AskTurnOffState) =>
  state.phase === 'working'
  || (state.phase === 'ready' && (Boolean(state.setup) || (Boolean(state.failure) && state.failure !== 'cancelled' && state.failure !== 'unavailable')));

/** A fixed state for every switch below it (design frames draw each state from this). */
export const AskTurnOffStateOverride = createContext<AskTurnOffState | null>(null);

// An attempt belongs to the target it started on (`key`) and its own `id`: a late answer for an
// earlier target or attempt never reaches the switch now shown.
type Attempt = { key: string; id: number } & (
  | { phase: 'working'; settingUp?: boolean }
  | { phase: 'done'; failure?: PolicyTurnOffFailure; setup?: AskSetupOutcome }
);

/**
 * `enabled`: only ask the sandbox when there is something this switch could turn off.
 * `onDone` receives the server's current row after a successful turn-off.
 */
export function useAskTurnOff(target: { name: string; account?: number }, enabled: boolean, onDone: (secret: VaultSecret) => void) {
  const api = useApi();
  const override = useContext(AskTurnOffStateOverride);
  const [supported, setSupported] = useState<boolean | null>(null);
  const [stored, setAttempt] = useState<Attempt | null>(null);
  const { name, account } = target;
  const key = `${name}\u0000${account ?? ''}`;
  const current = useRef({ key, id: 0, live: true });
  useEffect(() => {
    current.current.key = key;
  }, [key]);
  useEffect(() => {
    const ref = current.current;
    ref.live = true;
    return () => { ref.live = false; };
  }, []);
  const attempt = stored?.key === key ? stored : null;

  useEffect(() => {
    if (!enabled || override) return;
    let cancelled = false;
    getVaultSandboxClient()
      .then((client) => client.supports(POLICY_DOWNGRADE_OP))
      .catch(() => false)
      .then((value) => { if (!cancelled) setSupported(value); });
    return () => { cancelled = true; };
  }, [enabled, override]);

  const turnOff = useCallback(() => {
    if (override) return;
    const ref = current.current;
    const id = ++ref.id;
    const started = key;
    // This click is the gesture: open the sandbox window before any await. Without that window
    // there is no confirmation: say the browser blocked it and ask nothing of the server.
    const authorizationWindow = openVaultAuthorizationWindow();
    if (!authorizationWindow) {
      setAttempt({ key: started, id, phase: 'done', failure: 'popup_blocked' });
      return;
    }
    setAttempt({ key: started, id, phase: 'working' });
    void turnOffAskBeforeSigning(api, getVaultSandboxClient, { name, account }, authorizationWindow.id)
      .then((outcome) => {
        if (!ref.live || ref.id !== id || ref.key !== started) return;
        if (outcome.ok) {
          setAttempt(null);
          onDone(outcome.secret);
          return;
        }
        if (outcome.reason === 'unavailable') setSupported(false);
        setAttempt({ key: started, id, phase: 'done', failure: outcome.reason === 'unavailable' ? undefined : outcome.reason });
      })
      .finally(() => authorizationWindow.close());
  }, [api, name, account, key, onDone, override]);

  // Only offered after the server said this instance never set one up. The sandbox shows its own
  // setup card; on success the Passkey is committed, and turning the switch off is asked again.
  const setup = useCallback(() => {
    if (override) return;
    const ref = current.current;
    const id = ++ref.id;
    const started = key;
    setAttempt({ key: started, id, phase: 'working', settingUp: true });
    void setupVaultDurably(api)
      .catch((error: unknown) => ({ ok: false as const, code: setupEndedByUser(error) ? 'cancelled' : 'setup_failed' }))
      .then((result) => {
        if (!ref.live || ref.id !== id || ref.key !== started) return;
        if (result.ok) setAttempt({ key: started, id, phase: 'done', setup: 'done' });
        else if (result.code === 'cancelled') setAttempt({ key: started, id, phase: 'done', failure: 'setup_required' });
        else if (result.code === 'vault_already_initialized') setAttempt({ key: started, id, phase: 'done', setup: 'conflict' });
        else setAttempt({ key: started, id, phase: 'done', failure: 'setup_required', setup: 'failed' });
      });
  }, [api, key, override]);

  let state: AskTurnOffState;
  if (override) state = override;
  else if (!enabled || supported === false) state = { phase: 'unavailable' };
  else if (supported === null) state = { phase: 'checking' };
  else if (attempt?.phase === 'working') state = { phase: 'working', settingUp: attempt.settingUp };
  else state = { phase: 'ready', failure: attempt?.phase === 'done' ? attempt.failure : undefined, setup: attempt?.phase === 'done' ? attempt.setup : undefined };
  return { state, turnOff, setup };
}
