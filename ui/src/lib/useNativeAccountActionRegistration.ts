import { useEffect } from 'react';

import {
  hasNativeCapability,
  setNativeAccountActionAvailable,
} from './native';

export function useNativeAccountActionRegistration(): void {
  useEffect(() => {
    if (!hasNativeCapability('accountPresentation', 2)) return undefined;

    let mounted = true;
    let registered = false;

    const register = () => {
      if (!mounted || registered) return;
      registered = true;
      void setNativeAccountActionAvailable(true);
    };

    const unregister = () => {
      if (!registered) return;
      registered = false;
      void setNativeAccountActionAvailable(false);
    };

    register();
    window.addEventListener('pagehide', unregister);
    window.addEventListener('pageshow', register);
    return () => {
      mounted = false;
      window.removeEventListener('pagehide', unregister);
      window.removeEventListener('pageshow', register);
      unregister();
    };
  }, []);
}
