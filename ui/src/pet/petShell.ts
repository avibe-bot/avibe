import { useEffect, useSyncExternalStore } from 'react';

import { onPetEvents, petBridge, type PetBinding, type PetIntent } from './petBridge';

/**
 * The pet window's shell state, held once per document outside React so the
 * setup-pending view and the pet surface share it across their remounts:
 *
 * - `binding`: the bound session (undefined until the shell has told us);
 * - the latest summon not yet acted on. A summon that arrives while setup is
 *   pending, or before the surface has mounted, waits here, so the user's
 *   hotkey is never lost to the page's own loading order.
 *
 * The shell owns the binding and orders every change to it by revision. The
 * page applies whatever the shell reports (`pet_ready()`, `pet:bound`, and the
 * answers to its own picks and clears) only when it is newer than what it
 * already applied, so no answer or event can be overtaken by an older one.
 * The page's own changes are sent one at a time, in the order the user made
 * them, and shown at once; when none is outstanding, the page shows exactly
 * what the shell last reported.
 *
 * Listeners are installed before `pet_ready()` is called, so nothing the shell
 * sends after it can be missed; anything it sent earlier is returned by it.
 */
let binding: string | null | undefined;
let reported: PetBinding | null = null;
// The page's own changes not yet answered by the shell, and the latest one.
let outstanding = 0;
let latestChange: string | null = null;
let changes: Promise<void> = Promise.resolve();
let pendingSummon: PetIntent | null = null;
let started = false;
// Bumped by every live summon, so a summon `pet_ready()` returns is used only
// if no live one has arrived since it was asked.
let liveSummons = 0;
const storeListeners = new Set<() => void>();
const summonListeners = new Set<() => void>();

const show = () => {
  const next = outstanding > 0 ? latestChange : reported?.binding;
  if (next === binding) return;
  binding = next;
  storeListeners.forEach((listener) => listener());
};

const report = (next: PetBinding) => {
  if (reported && next.revision <= reported.revision) return;
  reported = next;
  show();
};

const noteSummon = (intent: PetIntent) => {
  pendingSummon = intent;
  summonListeners.forEach((listener) => listener());
};

/** A change made by this page: shown now, sent after the ones before it. */
const change = (next: string | null, send: () => Promise<PetBinding>, failed: () => void) => {
  outstanding += 1;
  latestChange = next;
  show();
  changes = changes
    .then(send)
    .then(report, failed)
    .finally(() => {
      outstanding -= 1;
      show();
    });
};

const start = () => {
  if (started) return;
  started = true;
  onPetEvents({
    summon: (intent) => {
      liveSummons += 1;
      noteSummon(intent);
    },
    bound: report,
  });
  const liveSummonsAtRequest = liveSummons;
  void petBridge.ready().then((ready) => {
    report(ready);
    if (ready.summon_pending && liveSummons === liveSummonsAtRequest) noteSummon(ready.summon_pending.intent);
  }).catch(() => {
    // Without an answer the pet starts unbound; the shell's next report wins.
    if (!reported) report({ binding: null, revision: 0 });
  });
};

export const petShell = {
  /** Bind `sessionId`. If the shell cannot, the pet shows what it holds. */
  pick: (sessionId: string) => change(sessionId, () => petBridge.bind(sessionId), () => undefined),

  /**
   * Clear an invalid binding (compare-and-clear). Unlike a failed pick, a
   * failed clear is not undone: the session is archived, missing or read-only,
   * so showing it again would only re-run validation and clear it again, in a
   * loop. The page treats it as cleared until the shell reports a newer
   * binding; if the shell keeps it durably, the next load validates it the
   * same way before the pet uses it, so it is never sent to.
   */
  unbind: (sessionId: string) => {
    if (binding !== sessionId) return;
    change(null, () => petBridge.unbind(sessionId), () => {
      if (reported?.binding === sessionId) reported = { binding: null, revision: reported.revision };
    });
  },

  /** Take the waiting summon, if any. */
  takeSummon: (): PetIntent | null => {
    const intent = pendingSummon;
    pendingSummon = null;
    return intent;
  },

  /** The binding now, for callbacks that must not read a stale render. */
  currentBinding: (): string | null | undefined => binding,
};

export function usePetBinding(): string | null | undefined {
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
