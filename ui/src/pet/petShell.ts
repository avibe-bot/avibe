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
type DismissalOperation = { kind: 'add' | 'remove'; id: string };
let dismissalLog: DismissalOperation[] = [];
let readyLoaded = false;
let changes: Promise<void> = Promise.resolve();
let readyPromise: Promise<void> = Promise.resolve();
const normalizationAttempts = new WeakSet<PetSurfaceEpoch>();
let started = false;
let liveSummons = 0;
const storeListeners = new Set<() => void>();
const summonListeners = new Set<() => void>();

const applyOperation = (ids: string[], operation: DismissalOperation) => operation.kind === 'add'
  ? [...new Set([...ids, operation.id])] : ids.filter((id) => id !== operation.id);

const persistDismissed = async (sessionId: string) => {
  if (!readyLoaded || sessionId !== dismissedSession || sessionId !== legacyBinding?.binding) return;
  const ids = dismissedIds;
  if (ids.length === legacyDismissed.length && ids.every((id) => legacyDismissed.includes(id))) return;
  await petBridge.setDismissedIds(sessionId, ids);
  legacyDismissed = ids;
};

const queueDismissalWrite = (sessionId: string) => {
  changes = changes.then(() => persistDismissed(sessionId)).catch(() => undefined);
};

const loadDismissalBaseline = () => {
  if (!dismissedSession) return;
  const baseline = dismissedSession === legacyBinding?.binding ? legacyDismissed : [];
  dismissedIds = dismissalLog.reduce(applyOperation, baseline);
  if (readyLoaded) {
    dismissalLog = [];
    queueDismissalWrite(dismissedSession);
  }
};

const setMain = (next: string | null) => {
  if (binding === next) return;
  binding = next;
  // A failed lookup withdraws the conversation, not the user's preferences.
  if (next && next !== dismissedSession) {
    if (dismissedSession) dismissalLog = [];
    dismissedSession = next;
    loadDismissalBaseline();
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
    readyLoaded = true;
    loadDismissalBaseline();
    storeListeners.forEach((listener) => listener());
    if (ready.summon_pending && liveSummons === liveSummonsAtRequest) noteSummon(ready.summon_pending.intent);
  }).catch(() => undefined);
};

const recordDismissal = (operation: DismissalOperation) => {
  // Keep even a remove of an unknown id: it may exist in the pending baseline.
  if (!readyLoaded || !dismissedSession) dismissalLog.push(operation);
  dismissedIds = applyOperation(dismissedIds, operation);
  storeListeners.forEach((listener) => listener());
  if (readyLoaded && dismissedSession) queueDismissalWrite(dismissedSession);
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
      if (!readyLoaded || !epoch.isCurrent() || legacyBinding?.binding === 'ses_main') return;
      const confirmed = await petBridge.bindMain();
      // An IPC already sent may finish after authorization changes. It only
      // writes main; mirror the native storage owner without selecting a session.
      legacyBinding = confirmed;
      legacyDismissed = [];
      await persistDismissed('ses_main');
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
  addDismissed: (sessionId: string, id: string) => {
    if (sessionId !== binding) return;
    recordDismissal({ kind: 'add', id });
  },
  forgetDismissed: (id: string) => recordDismissal({ kind: 'remove', id }),
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
