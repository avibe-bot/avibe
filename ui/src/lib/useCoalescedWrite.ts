import { useCallback, useSyncExternalStore } from 'react';

import { useLatestRef } from './useLatestRef';

/** One resource's writer: at most one request in flight, at most one patch waiting. */
type Entry = {
  /** The write not yet sent, already merged with everything clicked before it. */
  pending: { patch: unknown } | undefined;
  send: (patch: unknown, key: string) => Promise<boolean>;
  merge: (prev: unknown, next: unknown) => unknown;
  onSettled: ((key: string, committed: boolean) => void | Promise<void>) | undefined;
  drainedWaiters: Array<() => void>;
};

// MODULE scope on purpose: a resource outlives the view that edits it. ChatPage
// unmounts when the user opens Inbox, so a hook-local queue would let the next
// visit to the same chat fire a second PATCH beside the first and let the older
// one commit last. Keys are namespaced per scope, so two owners writing
// different resource kinds cannot collide on an id.
const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();
// A snapshot of which resources are mid-write, rebuilt only when that set
// changes: `useSyncExternalStore` requires a stable reference between changes,
// and the identity is what re-renders consumers reading it through a memoized
// context value.
let savingSnapshot: ReadonlySet<string> = new Set<string>();

const publish = () => {
  savingSnapshot = new Set(entries.keys());
  listeners.forEach((listener) => listener());
};

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};

const getSavingSnapshot = () => savingSnapshot;

/** Drops every writer. For tests (module state outlives a render root) and for a
 *  hard document reset; never part of a normal write path. */
export const resetCoalescedWrites = () => {
  entries.clear();
  publish();
};

const drain = async (scopedKey: string, key: string) => {
  let committed = true;
  for (;;) {
    const entry = entries.get(scopedKey);
    if (!entry) break;
    const next = entry.pending;
    if (next) {
      entry.pending = undefined;
      let ok = false;
      try {
        ok = await entry.send(next.patch, key);
      } catch {
        ok = false;
      }
      // A failure does NOT drop what is waiting. Nothing here is derived from the
      // request that failed: a session PATCH has no compare-and-set, and a
      // project route derives its expected agent at send time from the last
      // SERVER-confirmed route, so the waiting patch is still the user's current
      // intent and still the right thing to send.
      if (!ok) committed = false;
      continue;
    }
    // Nothing left to send. Reconcile BEFORE releasing the key, so a pick made
    // during a rollback read coalesces into this writer instead of starting a
    // fresh burst against state the read is still rewriting — and so a caller
    // waiting on `whenDrained` gets the authoritative row, not just the ack.
    try {
      await entry.onSettled?.(key, committed);
    } catch {
      // Reconciliation is the owner's business; it reports its own failures.
    }
    if (entry.pending) {
      committed = true;
      continue;
    }
    entries.delete(scopedKey);
    const waiters = entry.drainedWaiters;
    entry.drainedWaiters = [];
    publish();
    waiters.forEach((resolve) => resolve());
    break;
  }
};

export type CoalescedWrite<P> = {
  /** Record one write for `key`: sent now when that key is idle, else merged into the patch waiting behind the request in flight. */
  write: (key: string, patch: P) => void;
  /** True from the first write for `key` until that key has been reconciled. */
  isSaving: (key: string) => boolean;
  /** Resolves once `key` has nothing waiting, nothing in flight, and nothing left to reconcile — immediately when it is idle. */
  whenDrained: (key: string) => Promise<void>;
};

/**
 * Persists the writes behind an optimistic surface, so the UI can move on the
 * click without letting two requests for the same resource race.
 *
 * The owner applies the change to its own state and hands the payload to
 * `write`; the network catches up behind it. One request per resource is in
 * flight at a time, because these payloads overwrite each other's fields — an
 * Agent pick carries that Agent's default model and effort, a project route is a
 * whole 5-field snapshot — and an earlier request landing last would undo the
 * newer pick.
 *
 * What is waiting is COALESCED rather than queued: the clicks a user makes while
 * a request is in flight are transit, not intent, so `merge` folds them into one
 * payload and only the result is sent. That is what makes the writer safe to
 * share between fields — a title save and a route pick land in one request
 * instead of taking each other hostage — and it is sound because nothing in the
 * payload is derived from an earlier request's success (see the failure note in
 * `drain`).
 *
 * `send` reports its own failure (banner / toast) and returns false; a throw
 * counts as false too. `onSettled` then runs once per burst, for the owner to
 * reconcile its optimistic state with the server (a re-read, or a revert), and
 * the resource stays `isSaving` until that reconciliation finishes.
 */
export function useCoalescedWrite<P>(
  /** Namespace for the keys, so two owners writing different resource kinds never share an entry. */
  scope: string,
  send: (patch: P, key: string) => Promise<boolean>,
  options?: {
    /** Fold a new patch into the one already waiting. Defaults to "the newer one wins", which is right for whole-snapshot payloads. */
    merge?: (prev: P, next: P) => P;
    onSettled?: (key: string, committed: boolean) => void | Promise<void>;
  },
): CoalescedWrite<P> {
  const savingKeys = useSyncExternalStore(subscribe, getSavingSnapshot, getSavingSnapshot);
  const sendRef = useLatestRef(send);
  const mergeRef = useLatestRef(options?.merge);
  const settledRef = useLatestRef(options?.onSettled);

  const write = useCallback(
    (key: string, patch: P) => {
      const scopedKey = `${scope}:${key}`;
      const send: Entry['send'] = (p, k) => sendRef.current(p as P, k);
      const merge: Entry['merge'] = (prev, next) =>
        mergeRef.current ? mergeRef.current(prev as P, next as P) : next;
      const onSettled: Entry['onSettled'] = (k, committed) => settledRef.current?.(k, committed);
      const existing = entries.get(scopedKey);
      if (existing) {
        // Latest writer wins: after a remount, the live owner's closures are the
        // ones that must send and reconcile — the unmounted one has no screen to
        // put the answer on.
        existing.send = send;
        existing.merge = merge;
        existing.onSettled = onSettled;
        existing.pending = {
          patch: existing.pending ? existing.merge(existing.pending.patch, patch) : patch,
        };
        return;
      }
      entries.set(scopedKey, { pending: { patch }, send, merge, onSettled, drainedWaiters: [] });
      publish();
      // Entered synchronously, so the first request is in flight within the click
      // that recorded it: owners open their read-ordering fence inside `send`,
      // and it must not miss a read that starts a microtask later.
      void drain(scopedKey, key);
    },
    [scope, sendRef, mergeRef, settledRef],
  );

  const isSaving = useCallback((key: string) => savingKeys.has(`${scope}:${key}`), [savingKeys, scope]);

  // Resolves on settle whether or not the burst committed: a caller waiting for
  // "what the UI is showing has reached the server, or failed loudly" must not
  // hang on the failure path.
  const whenDrained = useCallback(
    (key: string) => {
      const entry = entries.get(`${scope}:${key}`);
      if (!entry) return Promise.resolve();
      return new Promise<void>((resolve) => {
        entry.drainedWaiters.push(resolve);
      });
    },
    [scope],
  );

  return { write, isSaving, whenDrained };
}
