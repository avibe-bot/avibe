import { useEffect, useSyncExternalStore } from 'react';

import { onPetEvents, petBridge, type LayoutSnapshot, type PetBinding, type PetIntent } from './petBridge';
import { petPanel } from './petPanel';
import type { PetSurfaceEpoch } from './usePetSession';

/** Document-local main resolution and pending summons. Legacy shell bindings
 * remain readable for compatibility, but never select the conversation. */
const FIRST_LAYOUT_TIMEOUT_MS = 4000;
let binding: string | null = null;
let legacyBinding: PetBinding | null = null;
let pendingSummon: PetIntent | null = null;
let dismissedSession: string | null = null;
let dismissedIds: string[] = [];
let readyLoaded = false;
let changes: Promise<void> = Promise.resolve();
let readyPromise: Promise<void> = Promise.resolve();
const normalizationAttempts = new WeakSet<PetSurfaceEpoch>();
let started = false;
let liveSummons = 0;
const storeListeners = new Set<() => void>();
const summonListeners = new Set<() => void>();

const setMain = (next: string | null) => {
  if (binding === next) return;
  binding = next;
  // A failed lookup withdraws the conversation, not the user's preferences.
  if (next && next !== dismissedSession) {
    if (dismissedSession) dismissedIds = [];
    dismissedSession = next;
  }
  storeListeners.forEach((listener) => listener());
};

const noteSummon = (intent: PetIntent) => {
  pendingSummon = intent;
  summonListeners.forEach((listener) => listener());
};

const start = () => {
  if (started) return;
  started = true;
  onPetEvents({
    summon: (intent) => {
      liveSummons += 1;
      noteSummon(intent);
    },
    // The shell chose a layout (a drop, even one another document started, or
    // a display change).
    layout: drawSnapshot,
  });
  const liveSummonsAtRequest = liveSummons;
  // The page draws nothing until the shell's first layout; if that never
  // comes, it draws the default one after FIRST_LAYOUT_TIMEOUT.
  const waiting = window.setTimeout(petPanel.stopWaiting, FIRST_LAYOUT_TIMEOUT_MS);
  readyPromise = petBridge.ready().then((ready) => {
    window.clearTimeout(waiting);
    legacyBinding = ready;
    readyLoaded = true;
    // A frame may already be waiting for this document (it loaded mid-drop).
    // The reply is always acknowledged, even for a revision this document
    // already drew from an event: the shell may have taken it for loading then.
    if (ready.snapshot) {
      if (petPanel.applySnapshot(ready.snapshot)) acknowledge(ready.snapshot.revision);
      else acknowledge(petPanel.revision());
    } else petPanel.stopWaiting();
    if (ready.summon_pending && liveSummons === liveSummonsAtRequest) noteSummon(ready.summon_pending.intent);
  }).catch(() => {
    window.clearTimeout(waiting);
    petPanel.stopWaiting();
  });
};

/**
 * Draws a newer layout snapshot, then tells the shell this revision is drawn
 * so a frame waiting for it comes back. The shell answers with its latest
 * snapshot, which is drawn in turn if it is newer still (an event that never
 * reached this document).
 */
const drawSnapshot = (snapshot: LayoutSnapshot) => {
  if (petPanel.applySnapshot(snapshot)) acknowledge(snapshot.revision);
};

/** After the drawn revision has been painted, tell the shell. */
const acknowledge = (revision: number) => {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    void petBridge.settle(revision).then((latest) => { if (latest) drawSnapshot(latest); }).catch(() => undefined);
  }));
};

const setDismissed = (ids: string[]) => {
  dismissedIds = ids;
  storeListeners.forEach((listener) => listener());
};

export const petShell = {
  setMain,

  /** One main-only binding normalization attempt per authorized epoch. */
  normalizeMain: (epoch: PetSurfaceEpoch) => {
    if (epoch.binding !== 'ses_main' || normalizationAttempts.has(epoch)) return;
    normalizationAttempts.add(epoch);
    start();
    changes = changes.then(async () => {
      await readyPromise;
      if (!readyLoaded || !epoch.isCurrent() || legacyBinding?.binding === 'ses_main') return;
      const confirmed = await petBridge.bindMain();
      // An IPC already sent may finish after authorization changes. It only
      // writes main; mirror the native storage owner without selecting a session.
      legacyBinding = confirmed;
    }).catch(() => undefined); // A later epoch may retry; never loop in this one.
  },

  /** Take the waiting summon, if any. */
  takeSummon: (): PetIntent | null => {
    const intent = pendingSummon;
    pendingSummon = null;
    return intent;
  },

  /** The binding now, for callbacks that must not read a stale render. */
  currentBinding: (): string | null => binding,
  dismissed: (): string[] => dismissedIds,
  // Document-local preferences stay with the last resolved main through an
  // outage. They never wait for pet_ready and never reach native storage.
  addDismissed: (id: string) => {
    if (!dismissedIds.includes(id)) setDismissed([...dismissedIds, id]);
  },
  forgetDismissed: (id: string) => {
    if (dismissedIds.includes(id)) setDismissed(dismissedIds.filter((entry) => entry !== id));
  },
};

export function usePetBinding(): string | null {
  useEffect(start, []);
  return useSyncExternalStore(
    (listener) => {
      storeListeners.add(listener);
      return () => storeListeners.delete(listener);
    },
    () => binding,
  );
}

/**
 * Call `listener` whenever a summon is waiting, including one that arrived
 * before this component mounted. The listener decides whether to take it.
 */
export function useOnSummon(listener: () => void): void {
  useEffect(() => {
    start();
    summonListeners.add(listener);
    // A summon that is already waiting is delivered after mount, never during it.
    if (pendingSummon !== null) queueMicrotask(() => {
      if (summonListeners.has(listener) && pendingSummon !== null) listener();
    });
    return () => {
      summonListeners.delete(listener);
    };
  }, [listener]);
}

export function usePetDismissed(): string[] {
  return useSyncExternalStore(
    (listener) => { storeListeners.add(listener); return () => { storeListeners.delete(listener); }; },
    () => dismissedIds,
  );
}
