import { createContext, useContext } from 'react';

import type { DockPin } from './dockDoc';

// The Dock value contract and the context handle every consumer reads.
// `DockProvider.tsx` owns the fetch/optimistic-write side; keeping the handle in
// its own component-free module lets the provider hot-reload on its own.
export interface DockValue {
  /** Reconciled docked subset (built-in ids + `show:<id>` pins), in user order. */
  order: string[];
  /** Installed AI pages (built-ins are implicitly installed, not listed here). */
  pins: DockPin[];
  /** Whether a session's Show Page is installed (pinned). */
  isPinned: (sessionId: string) => boolean;
  /** Whether a Dock id (built-in or `show:<id>`) is currently in the Dock. */
  isDocked: (dockId: string) => boolean;
  /** The pin record for a session, or null. */
  pinFor: (sessionId: string) => DockPin | null;
  /** Install a session's Show Page — also docks it (optimistic; idempotent). */
  pin: (sessionId: string) => Promise<void>;
  /** Uninstall a session's Show Page — removes it from install + Dock (optimistic; idempotent). */
  unpin: (sessionId: string) => Promise<void>;
  /** Add a known tile (built-in or installed page) to the Dock (optimistic; idempotent). */
  dock: (dockId: string) => Promise<void>;
  /** Remove a tile from the Dock, keeping it installed (optimistic; idempotent). */
  undock: (dockId: string) => Promise<void>;
  /** Persist a new resident-tile order (optimistic; rolls back if rejected). */
  setOrder: (order: string[]) => Promise<void>;
}


export const DockContext = createContext<DockValue | null>(null);

export function useDock(): DockValue {
  const ctx = useContext(DockContext);
  if (!ctx) throw new Error('useDock must be used within a DockProvider');
  return ctx;
}
