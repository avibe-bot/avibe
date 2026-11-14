import { useCallback, useEffect, useRef, useState } from 'react';
import {
  DESKTOP_CLIENT_STATE_EVENT, hasDesktopClient, isClientState, isNewerClientState, withLocalIdentity,
  type DesktopClientAction, type DesktopClientReply, type DesktopClientRequest, type DesktopClientState, type SignOutIntent,
} from './bridge';

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
  const send = useCallback(async (action: DesktopClientAction): Promise<DesktopClientReply | null> => {
    if (action.type !== 'get_state') { actionSequence.current += 1; setIntent(null); }
    const sequence = actionSequence.current;
    const internals = (window as unknown as {
      __TAURI_INTERNALS__?: { invoke: (command: string, args: { request: DesktopClientRequest }) => Promise<DesktopClientReply> };
    }).__TAURI_INTERNALS__;
    if (!hasDesktopClient() || !internals?.invoke) { setError('bridge_unavailable'); return null; }
    const request: DesktopClientRequest = {
      version: 1, request_id: crypto.randomUUID(), expected_generation: current.current?.generation ?? 0, action,
    };
    setError(null);
    try {
      const reply = await internals.invoke('desktop_client_request', { request });
      if (!alive.current) return null;
      if (reply.version !== 1 || reply.request_id !== request.request_id || !isClientState(reply.state)) {
        setError('bridge_incompatible'); return null;
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
      if (alive.current) setError('bridge_unavailable');
      return null;
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
