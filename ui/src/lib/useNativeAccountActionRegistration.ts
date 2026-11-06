import { useEffect, useEffectEvent, useRef } from 'react';

import {
  hasNativeCapability,
  setNativeAccountActionAvailable,
} from './native';

export function useNativeAccountActionRegistration(available: boolean): void {
  const mountedRef = useRef(false);
  const pageActiveRef = useRef(true);
  const publishAvailability = useEffectEvent((pageActive: boolean) => {
    void setNativeAccountActionAvailable(pageActive && available);
  });

  useEffect(() => {
    if (!hasNativeCapability('accountPresentation', 2)) return undefined;

    const handlePageHide = () => {
      pageActiveRef.current = false;
      publishAvailability(false);
    };
    const handlePageShow = () => {
      pageActiveRef.current = true;
      publishAvailability(true);
    };

    pageActiveRef.current = true;
    publishAvailability(true);
    window.addEventListener('pagehide', handlePageHide);
    window.addEventListener('pageshow', handlePageShow);
    return () => {
      window.removeEventListener('pagehide', handlePageHide);
      window.removeEventListener('pageshow', handlePageShow);
      void setNativeAccountActionAvailable(false);
    };
  }, []);

  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    if (!hasNativeCapability('accountPresentation', 2)) return;
    publishAvailability(pageActiveRef.current);
  }, [available]);
}
