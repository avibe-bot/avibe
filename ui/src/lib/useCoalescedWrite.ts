import { useCallback, useEffect, useMemo, useSyncExternalStore } from 'react';

import { useLatestRef } from './useLatestRef';

/** The surface a scope's writes belong to: one live view, whichever is mounted. */
type Owner = {
  send: (patch: unknown, key: string) => Promise<boolean>;
  merge: (prev: unknown, next: unknown) => unknown;
  standsAlone: (patch: unknown, key: string) => boolean;
  onSettled: ((key: string, committed: boolean) => void | Promise<void>) | undefined;
};

/** One resource's writer: at most one request in flight, at most one patch waiting. */
type Entry = {
  /** The write not yet sent, already merged with everything clicked before it. */
  pending: { patch: unknown } | undefined;
  /** Who serves this burst — refreshed from `owners`, never fixed at creation. */
  owner: Owner;
};

// MODULE scope on purpose: a resource outlives the view that edits it. ChatPage
// unmounts when the user opens Inbox, so a hook-local queue would let the next
// visit to the same chat fire a second PATCH beside the first and let the older
// one commit last. Keys are namespaced per scope, so two owners writing
// different resource kinds cannot collide on an id.
const entries = new Map<string, Entry>();
// A scope names ONE owning surface, and the burst is served by whichever mount
// of it is up now. A burst outlives its view — the user navigates away
// mid-request and comes back — and the answer has to land on the screen that is
// there when it arrives: reconciling into an unmounted page leaves the live one
// showing state nobody converged. Registered on mount and deliberately never
// removed on unmount, because a request in flight still needs a `send`; the next
// mount replaces it.
const owners = new Map<string, Owner>();
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
  owners.clear();
  publish();
};

const drain = async (scope: string, scopedKey: string, key: string) => {
  let committed = true;
  for (;;) {
    const entry = entries.get(scopedKey);
    if (!entry) break;
    // Re-read the owner on every step, never captured at burst start: each
    // request and the settle after it belong to the view that is mounted when
    // they happen.
    entry.owner = owners.get(scope) ?? entry.owner;
    // A failure ends the burst UNLESS what waits stands on its own. Only the owner
    // can say: the dependency may be the payload's own fields, which the writer
    // never inspects, or a precondition the SENDER derives for it — and the answer
    // is asked for here, after the failure, so it can account for what that failure
    // said. The default answer is the safe one. See the block below the send.
    const next =
      entry.pending && (committed || entry.owner.standsAlone(entry.pending.patch, key))
        ? entry.pending
        : undefined;
    if (next) {
      entry.pending = undefined;
      try {
        committed = await entry.owner.send(next.patch, key);
      } catch {
        committed = false;
      }
      continue;
    }
    // Nothing more goes out in this burst — either nothing is waiting, or a send
    // failed and what waits was COMPOSED AGAINST the state that request was
    // installing. Those patches are partial by nature (the picker emits an effort
    // click as `{reasoning_effort}` alone), so sending one now would apply it to a
    // route that never existed — an effort chosen for the Agent the failed write
    // was switching to, landing on the Agent the row still holds. Dropping it is
    // what makes the rollback whole: the reconcile below reverts the burst
    // together, so the user sees the row the server holds instead of a
    // combination nobody picked.
    //
    // A payload that carries its whole resource was composed against nothing, so
    // a refusal says nothing about it: it is the user's newest intent, still
    // coherent on its own, and dropping it would lose a click for no reason. Those
    // keep the burst going above, and `committed` then reports whichever send
    // ended it. Unless the refusal invalidated a precondition the sender would
    // derive for it — a stale compare-and-set token makes the follow-up a
    // guaranteed conflict — which is why the owner answers with the failure in
    // hand rather than from the payload alone.
    entry.pending = undefined;
    // Reconcile BEFORE releasing the key, so a pick made during a rollback read
    // coalesces into this writer instead of starting a fresh burst against state
    // the read is still rewriting.
    try {
      await entry.owner.onSettled?.(key, committed);
    } catch {
      // Reconciliation is the owner's business; it reports its own failures.
    }
    // A pick made during a successful burst's reconcile is still live intent. One
    // made during a FAILED burst's rollback was composed against the state being
    // rolled back, so it goes the same way as the rest.
    if (committed && entry.pending) continue;
    entry.pending = undefined;
    entries.delete(scopedKey);
    publish();
    break;
  }
};

export type CoalescedWrite<P> = {
  /**
   * Record one write for `key`: sent now when that key is idle, else merged into
   * the patch waiting behind the request in flight.
   *
   * Returns whether this write OPENED a burst. That is the one moment at which
   * the state the burst is about to replace is still what the owner holds, so an
   * owner that has to revert a rejected burst captures its base on `true` and
   * accumulates into it on `false`.
   */
  write: (key: string, patch: P) => boolean;
  /** True from the first write for `key` until that key has been reconciled. */
  isSaving: (key: string) => boolean;
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
 * payload and only the result is sent. Serializing, coalescing and ending a burst
 * are all per KEY, so a key is a claim that its payloads overwrite each other:
 * fields that are composed against nothing in common belong to different keys, or
 * a refused write for one would discard a write for the other (the session row
 * splits its route from its title for exactly this reason).
 *
 * `send` reports its own failure (banner / toast) and returns false; a throw
 * counts as false too. A failure ends the burst and drops what was waiting, which
 * was composed against the state the server just refused — unless the owner's
 * `standsAlone` says that payload carries its whole resource, in which case the
 * refusal says nothing about it and the burst goes on (see `drain`). `onSettled`
 * then runs once per burst, for the owner to reconcile its optimistic state with
 * the server (a re-read, or a revert), and the resource stays `isSaving` until
 * that reconciliation finishes.
 *
 * `committed` is the outcome of the burst's LAST send, not a claim that nothing
 * was persisted: a burst commits in parts when one request lands and the patch
 * folded in behind it is refused. How much survived is the owner's to know — the
 * writer never inspects a payload's fields — so an owner that reverts a rejected
 * burst must advance its rollback target as each send commits, or it will undo a
 * change the server is holding.
 *
 * A burst is served by the LIVE owner of its scope, not by the mount that opened
 * it: navigating away mid-request and back hands the remaining request and the
 * settle to the view that is on screen when they happen. A scope therefore names
 * one owning surface.
 */
export function useCoalescedWrite<P>(
  /** Namespace for the keys, so two owners writing different resource kinds never share an entry. */
  scope: string,
  send: (patch: P, key: string) => Promise<boolean>,
  options?: {
    /** Fold a new patch into the one already waiting. Defaults to "the newer one wins", which is right for whole-snapshot payloads. */
    merge?: (prev: P, next: P) => P;
    /**
     * Whether a payload waiting behind a REFUSED request may still be sent: true
     * when nothing it depends on was invalidated by that refusal, so it is
     * coherent whatever the server just did. Defaults to false — a partial patch
     * applied to state the server kept would persist a combination nobody picked,
     * and that is the failure worth being conservative about.
     *
     * Asked AFTER the failure, and answered by the owner, because a dependency is
     * not always visible in the payload: the fields it carries, but also the
     * preconditions the sender will derive for it (a compare-and-set token, say)
     * and whatever the failure just said about them.
     */
    standsAlone?: (pending: P, key: string) => boolean;
    onSettled?: (key: string, committed: boolean) => void | Promise<void>;
  },
): CoalescedWrite<P> {
  const savingKeys = useSyncExternalStore(subscribe, getSavingSnapshot, getSavingSnapshot);
  const sendRef = useLatestRef(send);
  const mergeRef = useLatestRef(options?.merge);
  const standsAloneRef = useLatestRef(options?.standsAlone);
  const settledRef = useLatestRef(options?.onSettled);

  // One owner object per mount, whose methods read THIS mount's newest closures.
  // The refs are stable, so it is built once and its identity is what the
  // registration below hands over.
  const owner = useMemo<Owner>(
    () => ({
      send: (patch, key) => sendRef.current(patch as P, key),
      merge: (prev, next) => (mergeRef.current ? mergeRef.current(prev as P, next as P) : next),
      standsAlone: (patch, key) => standsAloneRef.current?.(patch as P, key) ?? false,
      onSettled: (key, committed) => settledRef.current?.(key, committed),
    }),
    [sendRef, mergeRef, standsAloneRef, settledRef],
  );

  useEffect(() => {
    owners.set(scope, owner);
  }, [scope, owner]);

  const write = useCallback(
    (key: string, patch: P): boolean => {
      const scopedKey = `${scope}:${key}`;
      const existing = entries.get(scopedKey);
      if (existing) {
        // The mount that is writing is by definition the one on screen, so it
        // takes the burst over — including its `merge`.
        existing.owner = owner;
        existing.pending = {
          patch: existing.pending ? owner.merge(existing.pending.patch, patch) : patch,
        };
        return false;
      }
      entries.set(scopedKey, { pending: { patch }, owner });
      publish();
      // Entered synchronously, so the first request is in flight within the click
      // that recorded it: owners open their read-ordering fence inside `send`,
      // and it must not miss a read that starts a microtask later.
      void drain(scope, scopedKey, key);
      return true;
    },
    [scope, owner],
  );

  const isSaving = useCallback((key: string) => savingKeys.has(`${scope}:${key}`), [savingKeys, scope]);

  return { write, isSaving };
}
