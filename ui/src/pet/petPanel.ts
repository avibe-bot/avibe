import { useCallback, useSyncExternalStore } from 'react';

import { petBridge, type PetLayout } from './petBridge';

const DEFAULT_LAYOUT: PetLayout = { panel_side: 'left', panel_edge: 'bottom' };

/**
 * The native frame's expansion, held once per document outside React so the
 * setup-pending card and the pet surface share it across their unmounts.
 *
 * Closing hides the panel at once (a moment of transparent frame is
 * harmless); opening shows it only once the shell has grown the frame, so
 * content is never marked seen while still clipped. Only the latest request
 * applies. A failed request restores the last confirmed expansion, so a
 * redundant expand of an already-open panel cannot hide it.
 */
let expanded = false;
let layout: PetLayout = DEFAULT_LAYOUT;
let requestId = 0;
let wantExpanded = false;
let confirmedExpanded = false;
const listeners = new Set<() => void>();

const notify = () => listeners.forEach((listener) => listener());

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

export const petPanel = {
  current: (): boolean => expanded,
  want: (): boolean => wantExpanded,
  layout: (): PetLayout => layout,
};

export function usePetPanel(): {
  expanded: boolean;
  layout: PetLayout;
  setPanel: (next: boolean) => Promise<void>;
} {
  const currentExpanded = useSyncExternalStore(subscribe, () => expanded);
  const currentLayout = useSyncExternalStore(subscribe, () => layout);
  const setPanel = useCallback(async (next: boolean) => {
    wantExpanded = next;
    const request = ++requestId;
    if (!next) {
      expanded = false;
      notify();
    }
    try {
      const applied = await petBridge.setExpanded(next);
      if (request !== requestId) return;
      confirmedExpanded = next;
      layout = applied;
      expanded = next;
      notify();
    } catch {
      if (request !== requestId) return;
      wantExpanded = confirmedExpanded;
      expanded = confirmedExpanded;
      notify();
    }
  }, []);
  return { expanded: currentExpanded, layout: currentLayout, setPanel };
}
