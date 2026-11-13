// The ask-before-signing switch's way off (contract v1.1): offered only when the pinned sandbox
// build lists the operation; every attempt is a fresh challenge, the popup opened in the click.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';

import { useApi, type VaultSecret } from '@/context/ApiContext';
import { getVaultSandboxClient, openVaultAuthorizationWindow } from './vaultSandboxClient';
import { POLICY_DOWNGRADE_OP, turnOffAskBeforeSigning, type PolicyTurnOffFailure } from './vaultSigningPolicy';

export type AskTurnOffState =
  | { phase: 'checking' }
  | { phase: 'unavailable' }
  | { phase: 'ready'; failure?: PolicyTurnOffFailure }
  | { phase: 'working' };

/** A fixed state for every switch below it (design frames draw each state from this). */
export const AskTurnOffStateOverride = createContext<AskTurnOffState | null>(null);

type Attempt = { phase: 'working' } | { phase: 'done'; failure?: PolicyTurnOffFailure } | null;

/**
 * `enabled`: only ask the sandbox when there is something this switch could turn off.
 * `onDone` receives the server's current row after a successful turn-off.
 */
export function useAskTurnOff(target: { name: string; account?: number }, enabled: boolean, onDone: (secret: VaultSecret) => void) {
  const api = useApi();
  const override = useContext(AskTurnOffStateOverride);
  const [supported, setSupported] = useState<boolean | null>(null);
  const [attempt, setAttempt] = useState<Attempt>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => { live.current = false; };
  }, []);

  useEffect(() => {
    if (!enabled || override) return;
    let cancelled = false;
    getVaultSandboxClient()
      .then((client) => client.supports(POLICY_DOWNGRADE_OP))
      .catch(() => false)
      .then((value) => { if (!cancelled) setSupported(value); });
    return () => { cancelled = true; };
  }, [enabled, override]);

  const { name, account } = target;
  const turnOff = useCallback(() => {
    if (override) return;
    // This click is the gesture: open the sandbox window before any await.
    const authorizationWindow = openVaultAuthorizationWindow();
    setAttempt({ phase: 'working' });
    void turnOffAskBeforeSigning(api, getVaultSandboxClient, { name, account }, authorizationWindow?.id)
      .then((outcome) => {
        if (!live.current) return;
        if (outcome.ok) {
          setAttempt(null);
          onDone(outcome.secret);
          return;
        }
        if (outcome.reason === 'unavailable') setSupported(false);
        setAttempt({ phase: 'done', failure: outcome.reason === 'unavailable' ? undefined : outcome.reason });
      })
      .finally(() => authorizationWindow?.close());
  }, [api, name, account, onDone, override]);

  let state: AskTurnOffState;
  if (override) state = override;
  else if (!enabled || supported === false) state = { phase: 'unavailable' };
  else if (supported === null) state = { phase: 'checking' };
  else if (attempt?.phase === 'working') state = { phase: 'working' };
  else state = { phase: 'ready', failure: attempt?.phase === 'done' ? attempt.failure : undefined };
  return { state, turnOff };
}
