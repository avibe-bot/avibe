import { useEffect } from 'react';

import { useApi } from '@/context/ApiContext';
import { canBringDesktopAppForward } from '@/lib/desktopShell';
import { watchVaultHandoffs } from '@/lib/vaultHandoffReturn';

/**
 * Brings the desktop app back when a Vaults step it handed to the browser has
 * finished there. Mounted once with the Workbench; renders nothing, and does
 * nothing outside a shell that can bring its window forward.
 */
export const VaultHandoffReturn = () => {
  const api = useApi();
  useEffect(() => (canBringDesktopAppForward() ? watchVaultHandoffs(api) : undefined), [api]);
  return null;
};
