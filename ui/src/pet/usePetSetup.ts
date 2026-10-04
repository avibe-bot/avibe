import { useCallback, useEffect, useRef, useState } from 'react';

import { useApi } from '@/context/ApiContext';
import { isSetupComplete } from '@/lib/setupState';

export type PetSetup = 'checking' | 'pending' | 'ready';

/**
 * Whether first-run setup is complete, for the pet window. Finishing the wizard
 * in `main` gives this window no route change and no event, so while setup is
 * pending the pet re-reads the authoritative config (past the config cache) on
 * every way the user comes back to it: a summon, focus, or becoming visible.
 * `recheck` is the summon hook.
 */
export function usePetSetup(): { setup: PetSetup; recheck: () => void } {
  const api = useApi();
  const [setup, setSetup] = useState<PetSetup>('checking');
  const requestRef = useRef(0);

  const check = useCallback(async () => {
    const request = ++requestRef.current;
    try {
      const config = await api.getConfig({ cache: false });
      if (request === requestRef.current) setSetup(isSetupComplete(config) ? 'ready' : 'pending');
    } catch {
      if (request === requestRef.current) setSetup('pending');
    }
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    const request = ++requestRef.current;
    api.getConfig({ cache: false }).then(
      (config) => {
        if (!cancelled && request === requestRef.current) setSetup(isSetupComplete(config) ? 'ready' : 'pending');
      },
      () => {
        if (!cancelled && request === requestRef.current) setSetup('pending');
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api]);

  useEffect(() => {
    if (setup !== 'pending') return undefined;
    const onFocus = () => void check();
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void check();
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [setup, check]);

  const recheck = useCallback(() => {
    if (setup === 'pending') void check();
  }, [setup, check]);

  return { setup, recheck };
}
