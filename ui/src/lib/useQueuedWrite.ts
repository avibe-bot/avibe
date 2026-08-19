import { useCallback, useRef, useState } from 'react';

import { useLatestRef } from './useLatestRef';

export type QueuedWrite<P> = {
  /** Queue one write: runs immediately when idle, else behind the ones ahead. */
  write: (patch: P) => void;
  /** True from the first queued write until the queue drains. */
  saving: boolean;
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
 * `send` reports its own failure (banner / toast) and returns false; a throw
 * counts as false too. A failed write drops whatever is still queued — those
 * writes were built on state the server never took — and `onSettled` then runs
 * once per burst, for the owner to reconcile its optimistic state with the
 * server (a re-read, or a revert).
 */
export function useQueuedWrite<P>(
  send: (patch: P) => Promise<boolean>,
  onSettled?: (committed: boolean) => void,
): QueuedWrite<P> {
  const [saving, setSaving] = useState(false);
  const queueRef = useRef<P[]>([]);
  const drainingRef = useRef(false);
  const sendRef = useLatestRef(send);
  const settledRef = useLatestRef(onSettled);

  const write = useCallback(
    (patch: P) => {
      queueRef.current.push(patch);
      if (drainingRef.current) return;
      drainingRef.current = true;
      setSaving(true);
      // Entered synchronously, so the first request is in flight within the click
      // that queued it: owners open their read-ordering fence inside `send`, and
      // it must not miss a read that starts a microtask later.
      void (async () => {
        let committed = true;
        try {
          while (queueRef.current.length > 0) {
            const next = queueRef.current.shift() as P;
            let ok = false;
            try {
              ok = await sendRef.current(next);
            } catch {
              ok = false;
            }
            if (!ok) {
              committed = false;
              break;
            }
          }
        } finally {
          queueRef.current = [];
          drainingRef.current = false;
          setSaving(false);
          settledRef.current?.(committed);
        }
      })();
    },
    [sendRef, settledRef],
  );

  return { write, saving };
}
