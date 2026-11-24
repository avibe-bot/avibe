import { useEffect, useState } from 'react';

import { devicesApi } from './api';

/**
 * Whether this instance has binding again at all, asked once. Until the instance says so (and on
 * a build before it, where there is no such route), the page says nothing that points there: a
 * sentence must not send anyone to an entry or a command that does not exist.
 */
export function useRebindOffered(): boolean {
  const [offered, setOffered] = useState(false);
  useEffect(() => {
    let current = true;
    void devicesApi.rebindOffered().then((answer) => { if (current) setOffered(answer); });
    return () => { current = false; };
  }, []);
  return offered;
}
