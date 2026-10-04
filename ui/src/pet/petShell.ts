import { useEffect, useSyncExternalStore } from 'react';

import { onPetEvents, petBridge, type PetIntent } from './petBridge';

/**
 * The pet window's shell state, held once per document outside React so the
 * setup-pending view and the pet surface share it across their remounts:
 *
 * - `binding`: the bound session (undefined until `pet_ready()` answers);
 * - the latest summon not yet acted on. A summon that arrives while setup is
 *   pending, or before the surface has mounted, waits here, so the user's
 *   hotkey is never lost to the page's own loading order.
 *
 * Listeners are installed before `pet_ready()` is called, so nothing the shell
 * sends after it can be missed; anything it sent earlier is returned by it.
 */
type Snapshot = { binding: string | null | undefined };

let snapshot: Snapshot = { binding: undefined };
let pendingSummon: PetIntent | null = null;
let started = false;
// Bumped by every binding change after start (a `pet:bound` event or this
// page's own pick or clear), so a `pet_ready()` answer captured before it
// cannot overwrite it.
let boundEvents = 0;
// Likewise for summons: the shell may deliver a live summon before this page
// has read the `pet_ready()` answer, and the older pending one must not win.
let liveSummons = 0;
// The binding the shell is known to hold: from `pet_ready()`, `pet:bound`, or a
// pick the shell acknowledged. A failed pick reverts to this, never to another
// unconfirmed pick.
let confirmed: string | null = null;
const storeListeners = new Set<() => void>();
const summonListeners = new Set<() => void>();

const setBindingState = (binding: string | null) => {
  if (snapshot.binding === binding) return;
  snapshot = { binding };
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
    bound: (sessionId) => {
      boundEvents += 1;
      confirmed = sessionId;
      setBindingState(sessionId);
    },
  });
  const boundEventsAtRequest = boundEvents;
  const liveSummonsAtRequest = liveSummons;
  const readyApplies = () => boundEvents === boundEventsAtRequest;
  void petBridge.ready().then((ready) => {
    if (readyApplies()) {
      confirmed = ready.binding;
      setBindingState(ready.binding);
    }
    if (ready.summon_pending && liveSummons === liveSummonsAtRequest) noteSummon(ready.summon_pending.intent);
  }).catch(() => {
    if (readyApplies()) setBindingState(null);
  });
};

export const petShell = {
  /** A binding change made by this page (a pick, a clear, or reverting one). */
  setBinding: (binding: string | null) => {
    boundEvents += 1;
    setBindingState(binding);
  },

  /** The shell acknowledged this binding. */
  confirm: (binding: string | null) => {
    confirmed = binding;
  },

  /** The last binding the shell is known to hold. */
  confirmedBinding: (): string | null => confirmed,

  /** Take the waiting summon, if any. */
  takeSummon: (): PetIntent | null => {
    const intent = pendingSummon;
    pendingSummon = null;
    return intent;
  },

  /** The binding now, for callbacks that must not read a stale render. */
  currentBinding: (): string | null | undefined => snapshot.binding,
};

export function usePetBinding(): string | null | undefined {
  useEffect(start, []);
  return useSyncExternalStore(
    (listener) => {
      storeListeners.add(listener);
      return () => storeListeners.delete(listener);
    },
    () => snapshot.binding,
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
