import { useEffect, useSyncExternalStore } from 'react';

import { onPetEvents, petBridge, type PetBinding, type PetIntent } from './petBridge';
import { petPanel } from './petPanel';
import type { PetSurfaceEpoch } from './usePetSession';

/** Document-local main resolution and pending summons. Legacy shell bindings
 * remain readable for compatibility, but never select the conversation. */
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
    // The shell moved the pet to a spot with the other layout: draw it, then
    // let the shell bring the window that fits it (as after a drop).
    layout: (next) => {
      petPanel.setLayout(next);
      requestAnimationFrame(() => requestAnimationFrame(() => { void petBridge.settle().catch(() => undefined); }));
    },
  });
  const liveSummonsAtRequest = liveSummons;
  readyPromise = petBridge.ready().then((ready) => {
    legacyBinding = ready;
    readyLoaded = true;
    if (ready.layout) petPanel.setLayout(ready.layout);
    if (ready.summon_pending && liveSummons === liveSummonsAtRequest) noteSummon(ready.summon_pending.intent);
  }).catch(() => undefined);
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
