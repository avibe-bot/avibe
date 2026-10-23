import { useCallback, useSyncExternalStore } from 'react';

import { petBridge, type LayoutSnapshot, type PetLayout } from './petBridge';

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
// The revision of `layout` (0: none from the shell yet).
let layoutRevision = 0;
// Under a shell that owns the layout, nothing is drawn until its first
// snapshot arrives, or until waiting for it is given up (`stopWaiting`).
let waitingGivenUp = false;
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
  /** Waiting for the shell's first layout: draw nothing yet (spec §9.1). */
  awaitingLayout: (): boolean => layoutRevision === 0 && !waitingGivenUp && petBridge.shellDrags(),
  /** `pet_ready` failed or never answered: draw the default layout. */
  stopWaiting: () => {
    if (waitingGivenUp || layoutRevision > 0) return;
    waitingGivenUp = true;
    notify();
  },
  /**
   * The shell's layout, its only source (spec §9.1). Only a newer revision is
   * drawn; answers whether it was.
   */
  applySnapshot: (snapshot: LayoutSnapshot): boolean => {
    if (snapshot.revision <= layoutRevision) return false;
    layoutRevision = snapshot.revision;
    if (snapshot.layout.panel_edge !== layout.panel_edge || snapshot.layout.panel_side !== layout.panel_side) {
      layout = snapshot.layout;
    }
    // Every accepted revision: what subscribers read also depends on it
    // (`awaitingLayout`), not only on the layout.
    notify();
    return true;
  },
};

export function usePetPanel(): {
  expanded: boolean;
  layout: PetLayout;
  /** Nothing is drawn while the shell's first layout is on its way. */
  awaiting: boolean;
  setPanel: (next: boolean) => Promise<void>;
} {
  const currentExpanded = useSyncExternalStore(subscribe, () => expanded);
  const currentLayout = useSyncExternalStore(subscribe, () => layout);
  const awaiting = useSyncExternalStore(subscribe, petPanel.awaitingLayout);
  const setPanel = useCallback(async (next: boolean) => {
    wantExpanded = next;
    const request = ++requestId;
    if (!next) {
      expanded = false;
      notify();
    }
    const run = async () => {
      try {
        await petBridge.setExpanded(next);
        if (request !== requestId) return;
        confirmedExpanded = next;
        // The reply's layout is for older pages; this one draws snapshots only.
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
  return { expanded: currentExpanded, layout: currentLayout, awaiting, setPanel };
}
