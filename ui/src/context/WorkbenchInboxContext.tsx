import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { useApi } from './ApiContext';
import type { InboxSession } from './ApiContext';

const PAGE_SIZE = 30;

interface InboxState {
  /** Per-session ("Slack-like") feed: one card per conversation, newest
   *  activity first. Driven by realtime ``inbox.session.updated`` upserts. */
  inboxSessions: InboxSession[];
  /** Pagination-independent per-session unread counts — the sidebar badges
   *  each session row from this (a session with unread may sit past the first
   *  inbox page, so the feed array alone isn't a complete source). */
  unreadBySession: Record<string, number>;
  /** Sum of ``unreadBySession`` — the Inbox nav badge. */
  totalUnread: number;
  /** Number of sessions with ≥1 unread reply — the header "N unread" count. */
  unreadSessions: number;
  /** Keyset cursor for "load more"; null when the feed is fully loaded. */
  nextCursor: string | null;
  loading: boolean;
  loadingMore: boolean;
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
  markRead: (sessionId: string, untilMessageId?: string) => Promise<void>;
}

const WorkbenchInboxContext = createContext<InboxState | undefined>(undefined);

// Sort matches the backend keyset order: last activity (any author) desc, then
// session_id desc as the stable tie-break, so client upserts stay consistent
// with server-paginated pages.
const byActivityDesc = (a: InboxSession, b: InboxSession): number => {
  if (a.last_activity_at !== b.last_activity_at) {
    return a.last_activity_at < b.last_activity_at ? 1 : -1;
  }
  if (a.session_id === b.session_id) return 0;
  return a.session_id < b.session_id ? 1 : -1;
};

const upsertSession = (list: InboxSession[], row: InboxSession): InboxSession[] => {
  const next = list.filter((s) => s.session_id !== row.session_id);
  next.push(row);
  next.sort(byActivityDesc);
  return next;
};

const appendPage = (prev: InboxSession[], page: InboxSession[]): InboxSession[] => {
  const seen = new Set(prev.map((s) => s.session_id));
  const merged = prev.concat(page.filter((s) => !seen.has(s.session_id)));
  merged.sort(byActivityDesc);
  return merged;
};

/** Provider that owns the Inbox state shared across WorkbenchSidebar + InboxPage.
 *
 *  Connects to ``/api/events`` and updates the per-session feed in place:
 *  ``inbox.session.updated``
 *  upserts + re-sorts a card (the realtime "bump to top"), ``inbox.unread.changed``
 *  refreshes the unread map after a mark-read elsewhere. Each (re)connect also
 *  does a full ``refresh()`` so events missed while the socket was down (the
 *  broker has no replay) are recovered. The provider value is memoized per
 *  [[feedback_react_context_value_memoize]] so consumer ``useEffect`` hooks that
 *  depend on context functions don't re-fire on every parent render. */
export const WorkbenchInboxProvider = ({ children }: { children: ReactNode }) => {
  const api = useApi();
  const [inboxSessions, setInboxSessions] = useState<InboxSession[]>([]);
  const [unreadBySession, setUnreadBySession] = useState<Record<string, number>>({});
  // Becomes true the first time the server hands us an authoritative whole-account
  // unread map. Until then ``totalUnread`` is only the empty-map default of 0,
  // which must NOT drive the app-icon badge: the push service worker may have set
  // a real badge while the app was closed, so a premature ``clearAppBadge()`` on a
  // slow or failed initial load would wipe a still-accurate count. (The realtime
  // session.updated/archived merges adjust a prior map, so they are not themselves
  // a first authoritative load and deliberately do not flip this.)
  const [unreadLoaded, setUnreadLoaded] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Mirror the cursor into a ref so ``loadMore`` can read the latest value
  // without re-creating its identity (and the context value) on every page.
  const cursorRef = useRef<string | null>(null);
  cursorRef.current = nextCursor;
  // Mirror the loaded feed so ``reconcile`` can size its re-read to the current
  // window without depending on (and re-identifying with) ``inboxSessions``.
  const inboxSessionsRef = useRef<InboxSession[]>([]);
  inboxSessionsRef.current = inboxSessions;
  // Only the very first mount does the destructive first-page refresh; every
  // later effect rerun — such as an ``api`` identity change after a locale switch
  // — reconciles the loaded window instead, so a non-resume
  // rerun never collapses a multi-page feed back to page one.
  const initialFetched = useRef(false);

  // One home for "an authoritative unread map arrived": set the map and flip
  // ``unreadLoaded`` together so the two can never drift apart. Every whole-account
  // write (refresh / reconcile / markRead / unread.changed) goes through here;
  // stable identity, so it never churns the memoized context value.
  const applyUnreadMap = useCallback((map: Record<string, number>) => {
    setUnreadBySession(map);
    setUnreadLoaded(true);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.listInbox({ platform: 'avibe', limit: PAGE_SIZE });
      setInboxSessions(result.sessions);
      setNextCursor(result.next_cursor);
      applyUnreadMap(result.unread_by_session ?? {});
    } catch (err) {
      console.error('[inbox] refresh failed', err);
    } finally {
      setLoading(false);
    }
  }, [api, applyUnreadMap]);

  const loadMore = useCallback(async () => {
    const cursor = cursorRef.current;
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const result = await api.listInbox({ platform: 'avibe', limit: PAGE_SIZE, before: cursor });
      setInboxSessions((prev) => appendPage(prev, result.sessions));
      setNextCursor(result.next_cursor);
    } catch (err) {
      console.error('[inbox] load more failed', err);
    } finally {
      setLoadingMore(false);
    }
  }, [api]);

  const markRead = useCallback(
    async (sessionId: string, untilMessageId?: string) => {
      const result = await api.markSessionRead(sessionId, untilMessageId);
      // The unread map is authoritative for badges; the card's unread styling
      // derives from it, so clearing here clears the dot without touching the
      // feed order (a read doesn't change last activity).
      applyUnreadMap(result.unread_by_session ?? {});
    },
    [api, applyUnreadMap],
  );

  // Resume reconcile: re-read the feed WITHOUT collapsing pagination. A
  // visibility/online resume can fire after the user has loaded several pages;
  // a plain first-page refresh() would drop every row past page 1 and reset the
  // cursor. Re-read enough rows to cover what's loaded (capped at the API's
  // 100-row max) and merge in place so existing rows update and any sessions
  // that arrived during the gap surface at top. No loading flag — the user
  // already has content; this is a silent catch-up.
  const reconcile = useCallback(async () => {
    // Snapshot loaded ids up front: sizes the re-read window, and lets us tell
    // afterward whether the read overlapped what we already had (cursor note).
    const loadedIds = new Set(inboxSessionsRef.current.map((s) => s.session_id));
    const limit = Math.min(Math.max(loadedIds.size, PAGE_SIZE), 100);
    try {
      const result = await api.listInbox({
        platform: 'avibe',
        limit,
        cache: false,
        handleError: false,
      });
      setInboxSessions((prev) => {
        const incoming = new Map(result.sessions.map((s) => [s.session_id, s]));
        const merged = prev.map((s) => incoming.get(s.session_id) ?? s);
        const have = new Set(prev.map((s) => s.session_id));
        for (const s of result.sessions) if (!have.has(s.session_id)) merged.push(s);
        merged.sort(byActivityDesc);
        return merged;
      });
      // Whole-account unread map (not paginated) — always authoritative.
      applyUnreadMap(result.unread_by_session ?? {});
      // Cursor: the loaded feed is always a contiguous run from the top, and
      // this reads the newest `limit` rows. If the read shares ANY row with what
      // we had (overlap), the two runs are contiguous — no gap below the read —
      // so the existing cursor still marks the boundary; leave it untouched
      // (this is what stops a >100-row exhausted feed from resurrecting a
      // duplicate-page "Load more"). If the read is ENTIRELY new rows (no
      // overlap), gap arrivals outnumbered the window and there are unseen rows
      // between this read and the old feed — adopt result.next_cursor so "Load
      // more" can page through them (loadMore dedupes the overlap).
      const overlap = result.sessions.some((s) => loadedIds.has(s.session_id));
      if (!overlap) setNextCursor(result.next_cursor);
    } catch (err) {
      console.error('[inbox] reconcile failed', err);
    }
  }, [api, applyUnreadMap]);

  useEffect(() => {
    // First mount loads page one; every later rerun reconciles the loaded window
    // instead when an ``api`` identity change rebuilds the value — so
    // a non-resume rerun never collapses a multi-page feed back to page one. The
    // broker fans events out live with no replay (sse_broker.py ``/api/events``),
    // so anything missed while the socket was down must be re-read; plain HTTP,
    // independent of whether the SSE stream itself comes back up.
    if (!initialFetched.current) {
      initialFetched.current = true;
      void refresh();
    } else {
      void reconcile();
    }
    const disconnect = api.connectWorkbenchEvents({
      onInboxSessionUpdated: (row) => {
        setInboxSessions((prev) => upsertSession(prev, row));
        setUnreadBySession((prev) => {
          if ((prev[row.session_id] ?? 0) === row.unread_count) return prev;
          const next = { ...prev };
          if (row.unread_count > 0) next[row.session_id] = row.unread_count;
          else delete next[row.session_id];
          return next;
        });
      },
      onInboxUnreadChanged: (data) => {
        if (data?.unread_by_session) {
          applyUnreadMap(data.unread_by_session);
        }
      },
      onSessionActivity: (data) => {
        // Terminal archive (here or in another tab) — drop the card + its unread
        // live, instead of waiting for the next reconnect/refresh to filter it.
        if (data.event !== 'archived') return;
        setInboxSessions((prev) => prev.filter((s) => s.session_id !== data.session_id));
        setUnreadBySession((prev) => {
          if (!(data.session_id in prev)) return prev;
          const next = { ...prev };
          delete next[data.session_id];
          return next;
        });
      },
      onError: (err) => {
        // ApiContext owns the explicit reconnect loop. Keep this a log, not a
        // crash, so the workbench stays usable during the HTTP fallback.
        console.debug('[inbox] sse error', err);
      },
    });
    return disconnect;
  }, [api, refresh, reconcile, applyUnreadMap]);

  // Recover after the OS suspended us. A backgrounded mobile PWA has its page
  // frozen and its SSE socket dropped, and the broker never replays the gap;
  // iOS can leave EventSource in a zombie OPEN state without onerror. ApiContext
  // reopens the shared stream on visibility, online, and focus; independently
  // reconcile the durable feed here so missed events never gate data freshness.
  useEffect(() => {
    const resync = () => {
      if (document.visibilityState === 'visible') void reconcile();
    };
    document.addEventListener('visibilitychange', resync);
    window.addEventListener('online', resync);
    window.addEventListener('focus', resync);
    return () => {
      document.removeEventListener('visibilitychange', resync);
      window.removeEventListener('online', resync);
      window.removeEventListener('focus', resync);
    };
  }, [reconcile]);

  const totalUnread = useMemo(
    () => Object.values(unreadBySession).reduce((sum, n) => sum + (n || 0), 0),
    [unreadBySession],
  );
  const unreadSessions = useMemo(
    () => Object.values(unreadBySession).filter((n) => (n || 0) > 0).length,
    [unreadBySession],
  );

  // Mirror the unread total onto the installed PWA's home-screen icon badge so
  // the icon matches the in-app Inbox badge. The push service worker (push-sw.js)
  // sets this while the app is closed; this keeps it live while the app is open —
  // reading clears it, a new reply bumps it. Best-effort + feature-detected:
  // browsers without the Badging API (and non-installed tabs) simply no-op, and a
  // rejected badge promise is swallowed so it never surfaces as an app error.
  //
  // Gated on ``unreadLoaded``: until the first authoritative unread map arrives,
  // ``totalUnread`` is just the default 0, and clearing here would wipe a badge
  // the service worker set while the app was closed if that initial load is slow,
  // fails, or redirects on an expired session. Once loaded, a real 0 clears it.
  useEffect(() => {
    const nav = navigator as Navigator & {
      setAppBadge?: (contents?: number) => Promise<void>;
      clearAppBadge?: () => Promise<void>;
    };
    if (!('setAppBadge' in nav)) return;
    if (!unreadLoaded) return;
    const op = totalUnread > 0 ? nav.setAppBadge?.(totalUnread) : nav.clearAppBadge?.();
    void op?.catch?.(() => {});
  }, [totalUnread, unreadLoaded]);

  const value = useMemo<InboxState>(
    () => ({
      inboxSessions,
      unreadBySession,
      totalUnread,
      unreadSessions,
      nextCursor,
      loading,
      loadingMore,
      refresh,
      loadMore,
      markRead,
    }),
    [
      inboxSessions,
      unreadBySession,
      totalUnread,
      unreadSessions,
      nextCursor,
      loading,
      loadingMore,
      refresh,
      loadMore,
      markRead,
    ],
  );

  return <WorkbenchInboxContext.Provider value={value}>{children}</WorkbenchInboxContext.Provider>;
};

export const useWorkbenchInbox = (): InboxState => {
  const ctx = useContext(WorkbenchInboxContext);
  if (ctx === undefined) {
    throw new Error('useWorkbenchInbox must be used inside <WorkbenchInboxProvider>');
  }
  return ctx;
};
