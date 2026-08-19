import { useCallback, useRef, useState } from 'react';

import { useLatestRef } from './useLatestRef';

const NO_SAVING_KEYS: ReadonlySet<string> = new Set<string>();

export type QueuedWrite<P> = {
  /** Queue one write for `key`: runs now when that key is idle, else behind the ones ahead of it. */
  write: (key: string, patch: P) => void;
  /** True from the first queued write for `key` until that key's queue drains. */
  isSaving: (key: string) => boolean;
  /** Resolves once `key` has nothing queued or in flight — immediately when it is idle. */
  whenDrained: (key: string) => Promise<void>;
};

/**
 * Serializes the writes behind an optimistic surface, so the UI can move on the
 * click without letting two requests for the same resource race.
 *
 * The owner applies the change to its own state and hands the payload to
 * `write`; the network catches up behind it. Ordering is why this exists instead
 * of firing each write on its own: these payloads overwrite each other's fields
 * — an Agent pick carries that Agent's default model and effort, a project
 * route is a whole 5-field snapshot — so an earlier request landing last would
 * undo the newer pick, and a compare-and-set write would be rejected outright.
 * Replaying them in click order leaves the server in the state the user
 * actually clicked their way to.
 *
 * Ordering is a property of one resource, so the queue is partitioned by `key`
 * (session id, project id): writes to different resources neither wait behind
 * each other nor share a fate. Owners outlive the resource they are editing —
 * the chat page survives navigating between sessions, one projects provider
 * serves every project — and a single queue would make a failed write to the
 * session the user just left drop the write to the one they are looking at.
 *
 * `send` reports its own failure (banner / toast) and returns false; a throw
 * counts as false too. A failed write drops whatever is still queued FOR THAT
 * KEY — those writes were built on state the server never took — and
 * `onSettled` then runs once per burst per key, for the owner to reconcile its
 * optimistic state with the server (a re-read, or a revert).
 */
export function useQueuedWrite<P>(
  send: (patch: P, key: string) => Promise<boolean>,
  onSettled?: (key: string, committed: boolean) => void,
): QueuedWrite<P> {
  const [savingKeys, setSavingKeys] = useState<ReadonlySet<string>>(NO_SAVING_KEYS);
  // A key present here is a key some drain loop owns: the loop deletes it only
  // once it stops, so `write` can tell "start a loop" from "append to one".
  const queuesRef = useRef(new Map<string, P[]>());
  const drainedWaitersRef = useRef(new Map<string, Array<() => void>>());
  const sendRef = useLatestRef(send);
  const settledRef = useLatestRef(onSettled);

  const write = useCallback(
    (key: string, patch: P) => {
      const queues = queuesRef.current;
      const queued = queues.get(key);
      if (queued) {
        queued.push(patch);
        return;
      }
      queues.set(key, [patch]);
      setSavingKeys((prev) => new Set(prev).add(key));
      // Entered synchronously, so the first request is in flight within the click
      // that queued it: owners open their read-ordering fence inside `send`, and
      // it must not miss a read that starts a microtask later.
      void (async () => {
        let committed = true;
        try {
          for (;;) {
            const queue = queues.get(key);
            if (!queue || queue.length === 0) break;
            const next = queue.shift() as P;
            let ok = false;
            try {
              ok = await sendRef.current(next, key);
            } catch {
              ok = false;
            }
            if (!ok) {
              committed = false;
              break;
            }
          }
        } finally {
          queues.delete(key);
          setSavingKeys((prev) => {
            if (!prev.has(key)) return prev;
            const next = new Set(prev);
            next.delete(key);
            return next;
          });
          settledRef.current?.(key, committed);
          const waiters = drainedWaitersRef.current.get(key);
          drainedWaitersRef.current.delete(key);
          waiters?.forEach((resolve) => resolve());
        }
      })();
    },
    [sendRef, settledRef],
  );

  const isSaving = useCallback((key: string) => savingKeys.has(key), [savingKeys]);

  // Resolves on settle whether or not the burst committed: a caller waiting for
  // "the route the UI is showing has reached the server, or failed loudly" must
  // not hang on the failure path.
  const whenDrained = useCallback((key: string) => {
    if (!queuesRef.current.has(key)) return Promise.resolve();
    return new Promise<void>((resolve) => {
      const waiters = drainedWaitersRef.current.get(key);
      if (waiters) waiters.push(resolve);
      else drainedWaitersRef.current.set(key, [resolve]);
    });
  }, []);

  return { write, isSaving, whenDrained };
}
