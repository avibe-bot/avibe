import { useEffect, useSyncExternalStore } from 'react';

import { onPetEvents, petBridge, type PetBinding, type PetIntent } from './petBridge';
import type { PetSurfaceEpoch } from './usePetSession';

/** Document-local main resolution and pending summons. Legacy shell bindings
 * remain readable for compatibility, but never select the conversation. */
let binding: string | null = null;
let legacyBinding: PetBinding | null = null;
let legacyDismissed: string[] = [];
let pendingSummon: PetIntent | null = null;
let dismissedSession: string | null = null;
let dismissedIds: string[] = [];
let dismissedEdited = false;
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
    dismissedSession = next;
    dismissedEdited = false;
    dismissedIds = next === legacyBinding?.binding ? legacyDismissed : [];
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
  });
  const liveSummonsAtRequest = liveSummons;
  readyPromise = petBridge.ready().then((ready) => {
    legacyBinding = ready;
    legacyDismissed = ready.dismissed_ids ?? [];
    if (dismissedSession && dismissedSession === ready.binding && !dismissedEdited) {
      dismissedIds = legacyDismissed;
      storeListeners.forEach((listener) => listener());
    }
    if (ready.summon_pending && liveSummons === liveSummonsAtRequest) noteSummon(ready.summon_pending.intent);
  }).catch(() => undefined);
};

const setDismissed = (sessionId: string, ids: string[]) => {
  if (sessionId !== dismissedSession) return;
  dismissedEdited = true;
  dismissedIds = ids;
  storeListeners.forEach((listener) => listener());
  // Normalization and preference writes share one queue. A temporary failed
  // main lookup does not change the owner of these document preferences.
  changes = changes.then(async () => {
    if (sessionId !== dismissedSession || sessionId !== legacyBinding?.binding) return;
    legacyDismissed = ids;
    await petBridge.setDismissedIds(sessionId, ids);
  }).catch(() => undefined);
};

export const petShell = {
  setMain,

  /** One normalization attempt per authorized main epoch, ordered before dismissals. */
  normalizeMain: (epoch: PetSurfaceEpoch) => {
    if (epoch.binding !== 'ses_main' || normalizationAttempts.has(epoch)) return;
    normalizationAttempts.add(epoch);
    start();
    changes = changes.then(async () => {
      await readyPromise;
      if (!epoch.isCurrent() || legacyBinding?.binding === 'ses_main') return;
      const confirmed = await petBridge.bindMain();
      // An IPC already sent may finish after authorization changes. It only
      // writes main; mirror the native storage owner without selecting a session.
      legacyBinding = confirmed;
      legacyDismissed = [];
      if (epoch.isCurrent() && dismissedIds.length > 0) {
        legacyDismissed = dismissedIds;
        await petBridge.setDismissedIds('ses_main', dismissedIds);
      }
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
  setDismissed: (sessionId: string, ids: string[]) => {
    if (sessionId !== binding) return;
    setDismissed(sessionId, ids);
  },
  forgetDismissed: (id: string) => {
    if (dismissedSession && dismissedIds.includes(id)) {
      setDismissed(dismissedSession, dismissedIds.filter((item) => item !== id));
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
