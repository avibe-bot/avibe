// Remembers which collapsible Workbench sidebar sections the user left open, so
// a reload or a new window keeps the layout they chose. A layout preference of
// this browser, not of the account: it is never sent to the server.
//
// Storage conventions mirror agentsViewMemory: versioned `avibe.*` key,
// injectable storage for tests, best-effort try/catch so private mode or
// blocked storage degrades to the default rather than throwing.
import { useCallback, useState } from 'react';

export type SidebarSection = 'capabilities' | 'projects';

const STORAGE_KEY = 'avibe.sidebar.sections.v1';

type ViewStorage = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(storage?: ViewStorage): ViewStorage | undefined {
  return storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
}

function readAll(storage?: ViewStorage): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(browserStorage(storage)?.getItem(STORAGE_KEY) ?? '{}');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Every section opens unless this browser recorded it closed: a missing,
 *  unknown, or corrupt value never hides navigation. */
export function readSidebarSectionOpen(section: SidebarSection, storage?: ViewStorage): boolean {
  return readAll(storage)[section] !== false;
}

export function writeSidebarSectionOpen(section: SidebarSection, open: boolean, storage?: ViewStorage): void {
  try {
    // Keep the other sections' entries, including ones a newer build wrote.
    browserStorage(storage)?.setItem(STORAGE_KEY, JSON.stringify({ ...readAll(storage), [section]: open }));
  } catch {
    // Layout memory is best-effort in private browsing and restricted storage contexts.
  }
}

/** A section's open state, read once on mount and recorded on every change. */
export function useSidebarSectionOpen(section: SidebarSection): readonly [boolean, (open: boolean) => void] {
  const [open, setOpenState] = useState(() => readSidebarSectionOpen(section));
  const setOpen = useCallback(
    (next: boolean) => {
      setOpenState(next);
      writeSidebarSectionOpen(section, next);
    },
    [section],
  );
  return [open, setOpen] as const;
}
