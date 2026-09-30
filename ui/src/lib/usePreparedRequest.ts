import { useCallback, useEffect, useRef } from 'react';

type Prepared<T> = {
  id: string;
  reply: Promise<T>;
  // When the request left, which bounds the reply's age from above: the daemon signs it later.
  sentAt: number | null;
  settled: boolean;
  failed: boolean;
  // A prepared request nobody claimed is dropped before it is sent once preparing stops.
  claimed: boolean;
  cancelled: boolean;
};

/**
 * Keeps one fresh reply to a daemon request ready while `key` is set, so a click can open the
 * sandbox authorization window first and then hand it the reply without a round trip. A Home
 * Screen app on iOS is frozen about two seconds after it opens a window, and WebKit blocks a window
 * opened after an await, so a round trip after the click often leaves that window on its
 * placeholder (protocol v2 §6.6).
 *
 * Requests run one at a time, so the latest one sent is the latest one the daemon saw. The reply is
 * refreshed every `refreshMs` while the page is visible, when the page becomes visible again with a
 * stale or failed reply, and whenever `key` changes. A claim stops the refresh and takes the latest
 * reply if it is for the same key, did not fail, and its request left at most `maxAgeMs` ago (or
 * has not left yet: any new request would queue behind it). Otherwise the claim sends a new request.
 */
export function usePreparedRequest<K, T>(
  key: K | null,
  send: (key: K) => Promise<T>,
  succeeded: (reply: T) => boolean,
  { maxAgeMs, refreshMs }: { maxAgeMs: number; refreshMs: number },
): (key: K) => Promise<T> {
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const prepared = useRef<Prepared<T> | null>(null);
  const stopRefresh = useRef<() => void>(() => undefined);
  const id = key === null ? null : JSON.stringify(key);

  const request = useCallback(
    (forKey: K): Prepared<T> => {
      const entry: Prepared<T> = {
        id: JSON.stringify(forKey),
        sentAt: null,
        settled: false,
        failed: false,
        claimed: false,
        cancelled: false,
        reply: queue.current
          .then(() => {
            if (entry.cancelled) throw new Error('prepared request cancelled');
            entry.sentAt = Date.now();
            return send(forKey);
          })
          .then(
            (reply) => {
              entry.settled = true;
              entry.failed = !succeeded(reply);
              return reply;
            },
            (error: unknown) => {
              entry.settled = true;
              entry.failed = true;
              throw error;
            },
          ),
      };
      queue.current = entry.reply.catch(() => undefined);
      return entry;
    },
    [send, succeeded],
  );

  useEffect(() => {
    if (id === null) return;
    const forKey = JSON.parse(id) as K;
    const isStale = (entry: Prepared<T>) => entry.failed || Date.now() - (entry.sentAt ?? 0) > refreshMs;
    const prepare = () => {
      const current = prepared.current;
      // A request still on its way stays the latest; another would only queue behind it.
      if (document.visibilityState !== 'visible' || (current && !current.settled)) return;
      prepared.current = request(forKey);
    };
    const onVisibilityChange = () => {
      const current = prepared.current;
      if (!current || (current.settled && isStale(current))) prepare();
    };
    prepare();
    const timer = window.setInterval(prepare, refreshMs);
    document.addEventListener('visibilitychange', onVisibilityChange);
    const stop = () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
    stopRefresh.current = stop;
    return () => {
      stop();
      if (prepared.current && !prepared.current.claimed) prepared.current.cancelled = true;
      prepared.current = null;
    };
  }, [id, request, refreshMs]);

  return useCallback(
    (forKey: K): Promise<T> => {
      stopRefresh.current();
      const entry = prepared.current;
      prepared.current = null;
      if (!entry) return request(forKey).reply;
      const usable =
        entry.id === JSON.stringify(forKey) && !entry.failed && (entry.sentAt === null || Date.now() - entry.sentAt <= maxAgeMs);
      if (usable) {
        entry.claimed = true;
        return entry.reply;
      }
      entry.cancelled = true;
      return request(forKey).reply;
    },
    [request, maxAgeMs],
  );
}
