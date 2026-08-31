import { createContext, useContext, useEffect, useLayoutEffect, useRef } from 'react';

export const RouteSurfaceActiveContext = createContext(true);

export const useRouteSurfaceActive = (): boolean => useContext(RouteSurfaceActiveContext);

export const useRouteSurfaceWindowEvent = <K extends keyof WindowEventMap>(
  type: K,
  listener: (event: WindowEventMap[K]) => void,
  enabled = true,
  options?: boolean | AddEventListenerOptions,
): void => {
  const active = useRouteSurfaceActive();
  const listenerRef = useRef(listener);

  useLayoutEffect(() => {
    listenerRef.current = listener;
  }, [listener]);

  useEffect(() => {
    if (!active || !enabled) return;
    const handler = (event: WindowEventMap[K]) => listenerRef.current(event);
    window.addEventListener(type, handler as EventListener, options);
    return () => window.removeEventListener(type, handler as EventListener, options);
  }, [active, enabled, options, type]);
};
