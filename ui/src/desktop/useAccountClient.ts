import { useCallback, useEffect, useRef, useState } from 'react';
import {
  DESKTOP_CLIENT_STATE_EVENT, hasDesktopClient, isClientState, isNewerClientState, withLocalIdentity,
  type DesktopClientAction, type DesktopClientReply, type DesktopClientRequest, type DesktopClientState, type SignOutIntent,
} from './bridge';

// A rejected invocation has no native reply. Keep this local outcome distinct
// from null (a retired operation); never fabricate a native envelope or state.
type ClientFailure = { kind: 'client_failure'; code: string; generation: number };

/** Only imported by the bundled account entry. No HTTP client or credential storage. */
export function useAccountClient() {
  const [state, setState] = useState<DesktopClientState | null>(() => {
    const initial = hasDesktopClient() && window.__AVIBE_DESKTOP_CLIENT_STATE__;
    return isClientState(initial) ? withLocalIdentity(initial) : null;
  });
  const current = useRef(state);
  const [intent, setIntent] = useState<SignOutIntent | null>(null);
  const actionSequence = useRef(0);
  const alive = useRef(true);
  const [error, setError] = useState<string | null>(null);
  const accept = useCallback((next: DesktopClientState) => {
    if (!alive.current || !isNewerClientState(next, current.current)) return false;
    current.current = withLocalIdentity(next);
    setState(current.current);
    return true;
  }, []);
  const send = useCallback(async (action: DesktopClientAction): Promise<DesktopClientReply | ClientFailure | null> => {
    if (action.type !== 'get_state') { actionSequence.current += 1; setIntent(null); }
    const sequence = actionSequence.current;
    const startingState = current.current;
    const generation = startingState?.generation ?? 0;
    const selectedId = action.type === 'select_instance' ? action.instance_id
      : action.type === 'decide_consent' && action.approve ? startingState?.consent?.instance_id : null;
    const fail = (code: string): ClientFailure | null => {
      if (!alive.current || sequence !== actionSequence.current) return null;
      const latest = current.current;
      // Selection itself advances generation. Only the same account's matching
      // native pending target proves that this invocation still owns that row.
      if (selectedId && latest && latest.generation !== generation
        && !(startingState?.account.identity && latest.account.status === 'signed_in'
          && latest.account.identity?.id === startingState?.account.identity?.id
          && latest.connection.status === 'switching'
          && latest.connection.pending?.instance_id === selectedId)) return null;
      setError(code);
      return { kind: 'client_failure', code, generation: latest?.generation ?? generation };
    };
    const internals = (window as unknown as {
      __TAURI_INTERNALS__?: { invoke: (command: string, args: { request: DesktopClientRequest }) => Promise<DesktopClientReply> };
    }).__TAURI_INTERNALS__;
    if (!hasDesktopClient() || !internals?.invoke) return fail('bridge_unavailable');
    const request: DesktopClientRequest = {
      version: 1, request_id: crypto.randomUUID(), expected_generation: generation, action,
    };
    setError(null);
    try {
      const reply = await internals.invoke('desktop_client_request', { request });
      if (!alive.current) return null;
      if (!reply || reply.version !== 1 || reply.request_id !== request.request_id || !isClientState(reply.state)
        || (reply.ok !== true && reply.ok !== false) || (!reply.ok && typeof reply.error?.code !== 'string')) {
        return fail('bridge_incompatible');
      }
      // Generation retires an operation; revision only orders its snapshot.
      // A broadcast may overtake a direct reply carrying one-time recovery codes.
      if (current.current && reply.state.generation < current.current.generation) return null;
      accept(reply.state);
      if (action.type === 'get_state' && sequence === actionSequence.current && reply.ok) {
        const direct = reply.result;
        setIntent(direct?.type === 'sign_out_intent' && typeof direct.intent_id === 'string'
          && direct.intent_id.length > 0 && direct.intent_id.length <= 128
          && Number.isSafeInteger(direct.generation) && direct.generation === current.current?.generation
          ? { intent_id: direct.intent_id, generation: direct.generation } : null);
      }
      if (!reply.ok) setError(reply.error.code);
      return reply;
    } catch {
      return fail('bridge_unavailable');
    }
  }, [accept]);
  useEffect(() => {
    alive.current = true;
    const update = (event: Event) => {
      const next: unknown = (event as CustomEvent).detail;
      if (isClientState(next)) accept(next);
    };
    window.addEventListener(DESKTOP_CLIENT_STATE_EVENT, update);
    // Subscribe before requesting the initial native snapshot.
    void Promise.resolve().then(() => { if (alive.current) void send({ type: 'get_state' }); });
    return () => { alive.current = false; window.removeEventListener(DESKTOP_CLIENT_STATE_EVENT, update); };
  }, [accept, send]);
  const signOutIntent = intent?.generation === state?.generation ? intent : null;
  return { state, error, send, signOutIntent };
}
