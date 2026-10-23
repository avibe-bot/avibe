import { useCallback, useSyncExternalStore } from 'react';

import { petBridge, type PetLayout } from './petBridge';

const DEFAULT_LAYOUT: PetLayout = { panel_side: 'left', panel_edge: 'bottom' };

/**
 * The native frame's expansion, held once per document outside React so the
 * setup-pending card and the pet surface share it across their unmounts.
 *
 * Closing hides the panel at once; opening waits for the shell to confirm
 * the panel state. The v2 window has a fixed size. Native state writes are sent
 * one at a time in the order they were asked, so an older `pet_set_expanded`
 * cannot apply after a newer one; only the latest request updates React. A
 * failed request restores the last confirmed expansion, so a redundant
 * expand of an already-open panel cannot hide it.
 */
let expanded = false;
let layout: PetLayout = DEFAULT_LAYOUT;
let requestId = 0;
let wantExpanded = false;
let confirmedExpanded = false;
let chain: Promise<void> = Promise.resolve();
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
  /** The shell chose a layout (on load, or where a drag dropped the pet). */
  setLayout: (next: PetLayout) => {
    if (next.panel_edge === layout.panel_edge && next.panel_side === layout.panel_side) return;
    layout = next;
    notify();
  },
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
    const run = async () => {
      try {
        const applied = await petBridge.setExpanded(next);
        if (request !== requestId) return;
        confirmedExpanded = next;
        // Only the shell chooses a layout; outside it there is none to apply.
        if (petBridge.native()) layout = applied;
        expanded = next;
        notify();
      } catch {
        if (request !== requestId) return;
        wantExpanded = confirmedExpanded;
        expanded = confirmedExpanded;
        notify();
      }
    };
    const wait = chain.then(run, run);
    chain = wait.then(() => undefined, () => undefined);
    await wait;
  }, []);
  return { expanded: currentExpanded, layout: currentLayout, setPanel };
}
