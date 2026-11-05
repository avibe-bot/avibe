import { useEffect } from 'react';

import { hasNativeCapability, setNativeOuterScrollLocked } from './native';

export function useNativeOuterScrollLock(): void {
  useEffect(() => {
    if (!hasNativeCapability('viewport')) return undefined;

    let mounted = true;
    let requested = false;

    const request = () => {
      if (!mounted || requested) return;
      requested = true;
      void setNativeOuterScrollLocked(true);
    };

    const release = () => {
      if (!requested) return;
      requested = false;
      void setNativeOuterScrollLocked(false);
    };

    request();
    window.addEventListener('pagehide', release);
    window.addEventListener('pageshow', request);
    return () => {
      mounted = false;
      window.removeEventListener('pagehide', release);
      window.removeEventListener('pageshow', request);
      release();
    };
  }, []);
}
