import type { ReactNode } from 'react';

import { useBots } from '@/context/BotsContext';

/**
 * Renders `children` once the Bots provider has its list. The product opens a Bot's dialog only
 * from a Bot the list already holds, and the dialog reads that Bot when it mounts, so a frame
 * that draws the dialog waits for the list the same way.
 */
export function WhenBotsListed({ children }: { children: ReactNode }) {
  return useBots().listed ? children : null;
}
