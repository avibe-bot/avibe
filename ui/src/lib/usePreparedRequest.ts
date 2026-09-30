import { useCallback, useEffect, useRef, useState } from 'react';

type Prepared<T> = {
  id: string;
  reply: Promise<T>;
  // When the request left, which bounds the reply's age from above: the daemon signs it later.
  sentAt: number | null;
  settled: boolean;
  failed: boolean;
  // Set once preparing stops, or a claim passes it over, before the request was sent.
  cancelled: boolean;
};

function isFresh<T>(entry: Prepared<T> | null, id: string, maxAgeMs: number): entry is Prepared<T> {
  return !!entry && entry.id === id && !entry.failed && (entry.sentAt === null || Date.now() - entry.sentAt <= maxAgeMs);
}

export type PreparedRequestOptions = {
  maxAgeMs: number;
  refreshMs: number;
  /**
   * The daemon honors only its most recent reply for a key, each request superseding the one before.
   * Requests then run one at a time and a claim takes the latest request, even one still on its way.
   * Otherwise requests run independently and a claim takes the newest reply that succeeded.
   */
  latestOnly: boolean;
};

/**
 * Keeps a fresh reply to a daemon request ready while `key` is set, for a click that opens the
 * sandbox authorization window. A Home Screen app on iOS is frozen about two seconds after it opens
 * a window, and WebKit blocks a window opened after an await, so that window gets its request in
 * time only if nothing it needs waits on the daemon after the click (protocol v2 §6.6).
 *
 * `pending` is true while the key has no reply yet: the click should wait for it rather than open
 * the window. A reply that failed ends `pending` too, so the click surfaces its error. The reply is
 * refreshed every `refreshMs` while the page is visible, and when the page becomes visible again
 * with a stale or failed reply. A claim stops the refresh and takes a reply for the same key that
 * did not fail and whose request left at most `maxAgeMs` ago; otherwise it sends a new request.
 */
export function usePreparedRequest<K, T>(
  key: K | null,
  send: (key: K) => Promise<T>,
  succeeded: (reply: T) => boolean,
  { maxAgeMs, refreshMs, latestOnly }: PreparedRequestOptions,
): { claim: (key: K) => Promise<T>; pending: boolean } {
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const latest = useRef<Prepared<T> | null>(null);
  const newestSucceeded = useRef<Prepared<T> | null>(null);
  // The reply a click took; preparing that stops afterwards must not drop it.
  const claimed = useRef<Prepared<T> | null>(null);
  const stopRefresh = useRef<() => void>(() => undefined);
  // The key being prepared now; a late reply for another key must not become its reply.
  const preparingId = useRef<string | null>(null);
  // The caller's latest callbacks, so a new function identity does not restart preparing.
  const sendRef = useRef(send);
  const succeededRef = useRef(succeeded);
  useEffect(() => {
    sendRef.current = send;
    succeededRef.current = succeeded;
  });
  // The key whose reply has settled since preparing last (re)started.
  const [settledId, setSettledId] = useState<string | null>(null);
  const id = key === null ? null : JSON.stringify(key);

  const request = useCallback(
    (forKey: K): Prepared<T> => {
      const entry: Prepared<T> = {
        id: JSON.stringify(forKey),
        sentAt: null,
        settled: false,
        failed: false,
        cancelled: false,
        reply: (latestOnly ? queue.current : Promise.resolve())
          .then(() => {
            if (entry.cancelled) throw new Error('prepared request cancelled');
            entry.sentAt = Date.now();
            return sendRef.current(forKey);
          })
          .then(
            (reply) => {
              entry.settled = true;
              entry.failed = !succeededRef.current(reply);
              const newest = newestSucceeded.current;
              if (!entry.failed && entry.id === preparingId.current && (!newest || (newest.sentAt ?? 0) <= (entry.sentAt ?? 0))) {
                newestSucceeded.current = entry;
              }
              return reply;
            },
            (error: unknown) => {
              entry.settled = true;
              entry.failed = true;
              throw error;
            },
          ),
      };
      if (latestOnly) queue.current = entry.reply.catch(() => undefined);
      latest.current = entry;
      return entry;
    },
    [latestOnly],
  );

  useEffect(() => {
    if (id === null) return;
    const forKey = JSON.parse(id) as K;
    let active = true;
    preparingId.current = id;
    const watch = (entry: Prepared<T>) => {
      const settle = () => {
        if (active && latest.current === entry) setSettledId(id);
      };
      entry.reply.then(settle, settle);
    };
    const inFlight = () => {
      const current = latest.current;
      return current && current.id === id && !current.settled ? current : null;
    };
    const prepare = () => {
      // A request still on its way stays the latest; another would only queue behind it.
      if (inFlight() || document.visibilityState !== 'visible') return;
      watch(request(forKey));
    };
    const onVisibilityChange = () => {
      const current = latest.current;
      if (!current || current.id !== id || (current.settled && (current.failed || Date.now() - (current.sentAt ?? 0) > refreshMs))) prepare();
    };
    // A reply still fresh from before keeps the key ready; otherwise the key waits for its reply.
    if (!isFresh(latestOnly ? latest.current : newestSucceeded.current, id, maxAgeMs)) {
      void Promise.resolve().then(() => {
        if (active) setSettledId((current) => (current === id ? null : current));
      });
    }
    const pendingRequest = inFlight();
    if (pendingRequest) watch(pendingRequest);
    else prepare();
    const timer = window.setInterval(prepare, refreshMs);
    document.addEventListener('visibilitychange', onVisibilityChange);
    const stop = () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
    stopRefresh.current = stop;
    return () => {
      active = false;
      preparingId.current = null;
      stop();
      const current = latest.current;
      if (current && current !== claimed.current && current.sentAt === null) current.cancelled = true;
    };
  }, [id, latestOnly, maxAgeMs, request, refreshMs]);

  const claim = useCallback(
    (forKey: K): Promise<T> => {
      stopRefresh.current();
      setSettledId(null);
      const forId = JSON.stringify(forKey);
      const inFlight = latest.current && latest.current.id === forId && !latest.current.settled ? latest.current : null;
      const candidate = latestOnly
        ? latest.current
        : isFresh(newestSucceeded.current, forId, maxAgeMs)
          ? newestSucceeded.current
          : inFlight;
      newestSucceeded.current = null;
      let taken = candidate;
      if (!taken || !isFresh(taken, forId, maxAgeMs)) {
        if (latest.current && latest.current.sentAt === null) latest.current.cancelled = true;
        taken = request(forKey);
      }
      claimed.current = taken;
      if (latestOnly) latest.current = null;
      return taken.reply;
    },
    [latestOnly, maxAgeMs, request],
  );

  return { claim, pending: id !== null && settledId !== id };
}
