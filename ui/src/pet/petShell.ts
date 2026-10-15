import { useEffect, useSyncExternalStore } from 'react';

import { onPetEvents, petBridge, type PetBinding, type PetIntent } from './petBridge';

/** Document-local main resolution and pending summons. Legacy shell bindings
 * remain readable for compatibility, but never select the conversation. */
let binding: string | null = null;
let legacyBinding: PetBinding | null = null;
let legacyDismissed: string[] = [];
let pendingSummon: PetIntent | null = null;
let dismissedIds: string[] = [];
let dismissedEdited = false;
let changes: Promise<void> = Promise.resolve();
let started = false;
let liveSummons = 0;
const storeListeners = new Set<() => void>();
const summonListeners = new Set<() => void>();

const setMain = (next: string | null) => {
  if (binding === next) return;
  binding = next;
  dismissedIds = next && next === legacyBinding?.binding ? legacyDismissed : [];
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
  });
  const liveSummonsAtRequest = liveSummons;
  void petBridge.ready().then((ready) => {
    legacyBinding = ready;
    legacyDismissed = ready.dismissed_ids ?? [];
    if (binding && binding === ready.binding && !dismissedEdited) {
      dismissedIds = legacyDismissed;
      storeListeners.forEach((listener) => listener());
    }
    if (ready.summon_pending && liveSummons === liveSummonsAtRequest) noteSummon(ready.summon_pending.intent);
  }).catch(() => undefined);
};

export const petShell = {
  setMain,

  /** Take the waiting summon, if any. */
  takeSummon: (): PetIntent | null => {
    const intent = pendingSummon;
    pendingSummon = null;
    return intent;
  },

  /** The binding now, for callbacks that must not read a stale render. */
  currentBinding: (): string | null => binding,
  dismissed: (): string[] => dismissedIds,
  setDismissed: (sessionId: string, ids: string[]) => {
    if (sessionId !== binding) return;
    dismissedEdited = true;
    dismissedIds = ids;
    storeListeners.forEach((listener) => listener());
    // Persist writes in order, so an older dismiss cannot resurrect a later one.
    // Native dismissal storage is still scoped to its legacy binding. Never
    // write that binding to make storage accept a different conversation.
    if (sessionId === legacyBinding?.binding) {
      legacyDismissed = ids;
      changes = changes.then(() => petBridge.setDismissedIds(sessionId, ids)).catch(() => undefined);
    }
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
