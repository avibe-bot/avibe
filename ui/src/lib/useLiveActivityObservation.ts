import { useCallback, useEffect, useRef } from 'react';
import { hasNativeCapability, nativeLiveActivityObserve, type NativeLiveActivityObservation } from './native';

/** The App owns activity lifetime; the page only reports the conversation on screen. */
export function useLiveActivityObservation(observation: NativeLiveActivityObservation) {
  const supported = hasNativeCapability('lockScreenProgress');
  const last = useRef<string | null>(null);
  const send = useCallback((next: NativeLiveActivityObservation) => {
    if (!supported) return;
    const key = JSON.stringify(next);
    if (key === last.current) return;
    last.current = key;
    void nativeLiveActivityObserve(next).catch((error: unknown) => {
      console.warn('Live Activity observation was not delivered', error);
    });
  }, [supported]);
  const { sessionId } = observation;
  const agentName = sessionId === null ? null : observation.agentName;
  const sessionTitle = sessionId === null ? null : observation.sessionTitle;
  const running = sessionId === null ? false : observation.running;
  useEffect(() => {
    send(sessionId === null ? { sessionId: null } : {
      sessionId, agentName: agentName!, sessionTitle, running,
    });
  }, [send, sessionId, agentName, sessionTitle, running]);
  useEffect(() => () => send({ sessionId: null }), [send]);
}
