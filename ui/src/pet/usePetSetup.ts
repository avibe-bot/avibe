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

  // One wake often triggers several checks at once (a summon focuses the
  // window). Finishing setup is one-way, so any check that sees it complete
  // wins, whatever order the answers land in. An incomplete answer applies
  // only from the latest check, and a failure only decides the first one:
  // it never turns a known state back.
  const check = useCallback(() => {
    const request = ++requestRef.current;
    return api.getConfig({ cache: false }).then(
      (config) => {
        if (isSetupComplete(config)) setSetup('ready');
        else if (request === requestRef.current) setSetup((current) => (current === 'ready' ? current : 'pending'));
      },
      () => setSetup((current) => (current === 'checking' ? 'pending' : current)),
    );
  }, [api]);

  useEffect(() => {
    void check();
  }, [check]);

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
