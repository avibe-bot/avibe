import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { useApi } from './ApiContext';
import type { InboxSession } from './ApiContext';
import { WorkbenchInboxContext, type InboxState } from './WorkbenchInboxContext';
import { sessionActivityInboxAction } from '../lib/inboxActivity';
import { syncFaviconBadge } from '../lib/faviconBadge';
import { useConsumerActivation } from '../lib/useConsumerActivation';
import {
  createWorkbenchSessionReadOwnership,
  type WorkbenchSessionReadStamp,
} from '../lib/workbenchSessionReadOwnership';

const PAGE_SIZE = 30;

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
  const incoming = new Map(page.map((row) => [row.session_id, row]));
  const merged = prev.map((row) => incoming.get(row.session_id) ?? row);
  const seen = new Set(prev.map((row) => row.session_id));
  for (const row of page) if (!seen.has(row.session_id)) merged.push(row);
  merged.sort(byActivityDesc);
  return merged;
};

type TargetedInboxSnapshot = {
  row: InboxSession | null;
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
  // Two different appetites behind one provider: the unread map badges the
  // favicon and the app icon on every route, while the paged feed is rendered
  // only by the sidebar and the Inbox page. Splitting them is what lets an admin
  // route keep its badge without paying for 30 rows of feed it never shows.
  const { active: feedActive, isActive: isFeedActive, activate: activateFeed } =
    useConsumerActivation();
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
  // "No whole-account map currently describes this account." The unread map is the
  // one thing here with no demand gate at all — every route badges it — so this is
  // the debt that keeps it live independently of which read was supposed to pay it:
  // true until one arrives, true again when an authorization change voids the scope
  // it described. Every read that can deliver a whole map is allowed to fail, be
  // invalidated, or be dropped by the demand gate; what must not happen is the debt
  // being forgotten with it. Distinct from ``unreadLoaded``, which is one-way and
  // answers a different question (may this document touch the OS badge at all).
  const [wholeUnreadOwed, setWholeUnreadOwed] = useState(true);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Mirror the cursor into a ref so ``loadMore`` can read the latest value
  // without re-creating its identity (and the context value) on every page.
  const cursorRef = useRef<string | null>(null);
  cursorRef.current = nextCursor;
  const applyNextCursor = useCallback((cursor: string | null) => {
    // A queued feed intent may start from the completing request's finally
    // block, before React renders the state update. Commit both owners together
    // so that intent always reads the logically latest cursor.
    cursorRef.current = cursor;
    setNextCursor(cursor);
  }, []);
  // Mirror the loaded feed so ``reconcile`` can size its re-read to the current
  // window without depending on (and re-identifying with) ``inboxSessions``.
  const inboxSessionsRef = useRef<InboxSession[]>([]);
  inboxSessionsRef.current = inboxSessions;
  // Only the very first mount does the destructive first-page refresh; every
  // later effect rerun — such as an ``api`` identity change after a locale switch
  // — reconciles the loaded window instead, so a non-resume
  // rerun never collapses a multi-page feed back to page one.
  const initialFetched = useRef(false);
  // Every Inbox read shares one ordering fence. This covers page-one refresh,
  // cursor reads, resume reconcile, and the targeted foreground-restore read.
  const readOwnershipRef = useRef(createWorkbenchSessionReadOwnership());
  // Broad reads and cursor reads both own the feed. Serialize every feed read
  // and preserve trailing intents so response completion order cannot discard a
  // recovery snapshot or a page the user asked to load.
  const refreshPendingRef = useRef(false);
  const reconcilePendingRef = useRef(false);
  const loadMorePendingRef = useRef(false);
  const broadReadInFlightRef = useRef(false);
  const cursorReadsInFlightRef = useRef(0);
  const refreshRunnerRef = useRef<() => void>(() => {});
  const reconcileRunnerRef = useRef<() => void>(() => {});
  const loadMoreRunnerRef = useRef<() => void>(() => {});
  const reconcileSessionRef = useRef<(sessionId: string) => void>(() => {});
  const refreshLoadingGenerationRef = useRef(0);
  const loadMoreLoadingGenerationRef = useRef(0);
  // Targeted foreground restores own one session independently from the
  // windowed feed. Keep their latest committed values so a later whole-feed
  // replacement cannot erase a restored card merely because the row sorts
  // outside the page; a later broad omission revalidates them by exact id.
  // Counts-only reads own no feed state, so they never join the feed read
  // intents above. They still serialize against themselves: a resume fires
  // visibilitychange, focus and online together, and one map is enough.
  const unreadReadInFlightRef = useRef(false);
  const unreadReadPendingRef = useRef(false);
  const targetedSnapshotsRef = useRef<Map<string, TargetedInboxSnapshot>>(new Map());
  const targetedReadInFlightRef = useRef<Set<string>>(new Set());
  const targetedReadPendingRef = useRef<Set<string>>(new Set());
  const committedWholeUnreadGenerationRef = useRef(0);

  const flushFeedReadIntent = useCallback(() => {
    if (broadReadInFlightRef.current || cursorReadsInFlightRef.current > 0) return;
    // Every intent queued here reads the FEED, so the demand gate applies to it
    // exactly as it applies to a first read: with no feed consumer left, a
    // 30-row page would be fetched for a document that renders a badge. Dropping
    // it loses nothing, because activation re-reads unconditionally (page one
    // when nothing was loaded, reconcile otherwise) — the same argument that
    // lets ``onConnected`` return early. It matters most right after
    // ``discardAuthorizedFeed``: invalidating the read in flight is itself what
    // queues a replacement refresh, which would repopulate the rows that branch
    // just dropped, off-route and in parallel with the counts-only read.
    //
    // That argument covers the FEED half of the dropped read. The whole-account
    // unread map it was also carrying is a different matter, because no route is
    // exempt from badging that — the debt is ``wholeUnreadOwed``, and the demand
    // edge that caused this drop is what makes the counts effect pay it.
    if (!isFeedActive()) {
      refreshPendingRef.current = false;
      reconcilePendingRef.current = false;
      loadMorePendingRef.current = false;
      return;
    }
    if (refreshPendingRef.current) {
      refreshPendingRef.current = false;
      // A replacement refresh owns the same top-of-feed recovery as a pending
      // reconcile, so one authoritative read satisfies both intents.
      reconcilePendingRef.current = false;
      refreshRunnerRef.current();
      return;
    }
    if (reconcilePendingRef.current) {
      reconcilePendingRef.current = false;
      reconcileRunnerRef.current();
      return;
    }
    if (loadMorePendingRef.current) {
      loadMorePendingRef.current = false;
      loadMoreRunnerRef.current();
    }
  }, [isFeedActive]);

  const queueRefreshIntent = useCallback(() => {
    refreshPendingRef.current = true;
    queueMicrotask(flushFeedReadIntent);
  }, [flushFeedReadIntent]);

  const queueReconcileIntent = useCallback(() => {
    reconcilePendingRef.current = true;
    queueMicrotask(flushFeedReadIntent);
  }, [flushFeedReadIntent]);

  const queueLoadMoreIntent = useCallback(() => {
    loadMorePendingRef.current = true;
    queueMicrotask(flushFeedReadIntent);
  }, [flushFeedReadIntent]);

  const acceptSessionMutation = useCallback((sessionId: string) => {
    readOwnershipRef.current.acceptMutation([
      'inbox-feed',
      'inbox-unread',
      `inbox-session:${sessionId}`,
      `inbox-unread-session:${sessionId}`,
    ]);
  }, []);

  const acceptUnreadMutation = useCallback(() => {
    readOwnershipRef.current.acceptMutation(['inbox-unread', 'inbox-unread-all']);
  }, []);

  // One home for "an authoritative unread map arrived": set the map and flip
  // ``unreadLoaded`` together so the two can never drift apart. Every whole-account
  // write (refresh / reconcile / unread.changed) goes through here; targeted
  // reads and mark-read responses merge one session without claiming completeness.
  // stable identity, so it never churns the memoized context value. Paying the
  // ``wholeUnreadOwed`` debt belongs here for the same reason: this is the only
  // place a whole map is adopted, so no producer has to remember to clear it.
  const applyUnreadMap = useCallback((map: Record<string, number>) => {
    setUnreadBySession(map);
    setUnreadLoaded(true);
    setWholeUnreadOwed(false);
  }, []);

  const applyWholeUnreadRead = useCallback((
    read: WorkbenchSessionReadStamp,
    map: Record<string, number>,
  ) => {
    committedWholeUnreadGenerationRef.current = Math.max(
      committedWholeUnreadGenerationRef.current,
      read.generation,
    );
    applyUnreadMap(map);
  }, [applyUnreadMap]);

  const applySessionUnread = useCallback((sessionId: string, count: number) => {
    setUnreadBySession((prev) => {
      const next = { ...prev };
      if (count > 0) next[sessionId] = count;
      else delete next[sessionId];
      return next;
    });
  }, []);

  const mergeTargetedSnapshots = useCallback((rows: InboxSession[]): InboxSession[] => {
    let next = rows;
    for (const [sessionId, snapshot] of targetedSnapshotsRef.current) {
      next = snapshot.row
        ? upsertSession(next, snapshot.row)
        : next.filter((row) => row.session_id !== sessionId);
    }
    return next;
  }, []);

  const acceptFeedRows = useCallback((
    read: WorkbenchSessionReadStamp,
    rows: InboxSession[],
    revalidateOmissions = false,
  ) => {
    const returnedIds = new Set(rows.map((row) => row.session_id));
    for (const row of rows) {
      const resource = `inbox-session:${row.session_id}`;
      readOwnershipRef.current.claimRead(read, resource);
      if (readOwnershipRef.current.isCurrent(read, resource)) {
        targetedSnapshotsRef.current.delete(row.session_id);
        targetedReadPendingRef.current.delete(row.session_id);
      }
    }
    if (!revalidateOmissions) return [];
    const omitted = new Set(
      Array.from(targetedSnapshotsRef.current.entries())
        .filter(([sessionId, snapshot]) => snapshot.row && !returnedIds.has(sessionId))
        .map(([sessionId]) => sessionId),
    );
    for (const sessionId of targetedReadInFlightRef.current) {
      if (!returnedIds.has(sessionId)) omitted.add(sessionId);
    }
    return Array.from(omitted);
  }, []);

  const refresh = useCallback(async function refresh() {
    if (broadReadInFlightRef.current || cursorReadsInFlightRef.current > 0) {
      queueRefreshIntent();
      return;
    }
    broadReadInFlightRef.current = true;
    refreshPendingRef.current = false;
    reconcilePendingRef.current = false;
    const read = readOwnershipRef.current.beginRead(['inbox-feed', 'inbox-unread', 'inbox-feed-refresh']);
    const loadingGeneration = ++refreshLoadingGenerationRef.current;
    const retryAfterInvalidation = () => {
      const invalidatedByMutation = !readOwnershipRef.current.isMutationCurrent(read, 'inbox-feed');
      const supersededByCursorRead =
        (readOwnershipRef.current.latestGeneration('inbox-feed-cursor') ?? 0) > read.generation;
      const supersededByRefresh =
        (readOwnershipRef.current.latestGeneration('inbox-feed-refresh') ?? 0) > read.generation;
      const supersededByReconcile =
        (readOwnershipRef.current.latestGeneration('inbox-feed-reconcile') ?? 0) > read.generation;
      if (
        (!invalidatedByMutation && !supersededByCursorRead) ||
        supersededByRefresh ||
        supersededByReconcile ||
        refreshPendingRef.current
      ) {
        return false;
      }
      queueRefreshIntent();
      return true;
    };
    setLoading(true);
    try {
      const result = await api.listInbox({ platform: 'avibe', limit: PAGE_SIZE });
      const feedCurrent = readOwnershipRef.current.isCurrent(read, 'inbox-feed');
      if (!feedCurrent) {
        retryAfterInvalidation();
      } else {
        const snapshotsToRevalidate = acceptFeedRows(read, result.sessions, true);
        setInboxSessions(() => mergeTargetedSnapshots(result.sessions));
        applyNextCursor(result.next_cursor);
        for (const sessionId of snapshotsToRevalidate) reconcileSessionRef.current(sessionId);
      }
      if (readOwnershipRef.current.isCurrent(read, 'inbox-unread')) {
        applyWholeUnreadRead(read, result.unread_by_session ?? {});
      }
    } catch (err) {
      if (!retryAfterInvalidation()) console.error('[inbox] refresh failed', err);
    } finally {
      if (refreshLoadingGenerationRef.current === loadingGeneration) setLoading(false);
      broadReadInFlightRef.current = false;
      flushFeedReadIntent();
    }
  }, [acceptFeedRows, api, applyNextCursor, applyWholeUnreadRead, flushFeedReadIntent, mergeTargetedSnapshots, queueRefreshIntent]);

  const loadMore = useCallback(async function loadMore() {
    if (
      broadReadInFlightRef.current ||
      refreshPendingRef.current ||
      reconcilePendingRef.current
    ) {
      queueLoadMoreIntent();
      return;
    }
    // A duplicate click while the same cursor is already loading is not a new
    // page intent. Mutation invalidation queues its retry explicitly below.
    if (cursorReadsInFlightRef.current > 0) return;
    loadMorePendingRef.current = false;
    const cursor = cursorRef.current;
    if (!cursor) return;
    const read = readOwnershipRef.current.beginRead(['inbox-feed', 'inbox-feed-cursor']);
    cursorReadsInFlightRef.current += 1;
    const loadingGeneration = ++loadMoreLoadingGenerationRef.current;
    let retryAfterInvalidation = false;
    setLoadingMore(true);
    try {
      const result = await api.listInbox({ platform: 'avibe', limit: PAGE_SIZE, before: cursor });
      if (!readOwnershipRef.current.isCurrent(read, 'inbox-feed')) {
        retryAfterInvalidation = readOwnershipRef.current.isLatestRead(read);
        return;
      }
      acceptFeedRows(read, result.sessions);
      setInboxSessions((prev) => mergeTargetedSnapshots(appendPage(prev, result.sessions)));
      applyNextCursor(result.next_cursor);
    } catch (err) {
      retryAfterInvalidation =
        !readOwnershipRef.current.isCurrent(read, 'inbox-feed') &&
        readOwnershipRef.current.isLatestRead(read);
      if (!retryAfterInvalidation) console.error('[inbox] load more failed', err);
    } finally {
      cursorReadsInFlightRef.current -= 1;
      if (loadMoreLoadingGenerationRef.current === loadingGeneration) setLoadingMore(false);
      if (retryAfterInvalidation) queueLoadMoreIntent();
      else flushFeedReadIntent();
    }
  }, [acceptFeedRows, api, applyNextCursor, flushFeedReadIntent, mergeTargetedSnapshots, queueLoadMoreIntent]);

  const markRead = useCallback(
    async (sessionId: string, untilMessageId?: string) => {
      // Clearing unread is best-effort, so a failure must not raise an error
      // toast. Reading a permitted session and mutating its unread state are
      // independent capabilities.
      const operation = readOwnershipRef.current.beginRead(`inbox-mark-read:${sessionId}`);
      const result = await api.markSessionRead(sessionId, untilMessageId, { handleError: false });
      if (
        !readOwnershipRef.current.isMutationCurrent(operation, `inbox-session:${sessionId}`)
      ) {
        return;
      }
      // A successful write commits after every read that was already in flight,
      // even when one of those reads started later and returns last. The endpoint
      // mutates only this session, so merge only that count; concurrent mark-read
      // writes for other sessions remain independent.
      readOwnershipRef.current.acceptMutation([
        'inbox-unread',
        `inbox-unread-session:${sessionId}`,
      ]);
      // The unread map is authoritative for badges; the card's unread styling
      // derives from it, so clearing here clears the dot without touching the
      // feed order (a read doesn't change last activity).
      if (!result?.unread_by_session) return;
      applyUnreadMap(result.unread_by_session);
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
    if (
      broadReadInFlightRef.current ||
      cursorReadsInFlightRef.current > 0 ||
      refreshPendingRef.current
    ) {
      queueReconcileIntent();
      return;
    }
    broadReadInFlightRef.current = true;
    reconcilePendingRef.current = false;
    const read = readOwnershipRef.current.beginRead(['inbox-feed', 'inbox-unread', 'inbox-feed-reconcile']);
    const retryAfterInvalidation = () => {
      const invalidatedByMutation = !readOwnershipRef.current.isMutationCurrent(read, 'inbox-feed');
      const supersededByCursorRead =
        (readOwnershipRef.current.latestGeneration('inbox-feed-cursor') ?? 0) > read.generation;
      const supersededByReconcileRead =
        (readOwnershipRef.current.latestGeneration('inbox-feed-reconcile') ?? 0) > read.generation;
      const supersededByRefresh =
        (readOwnershipRef.current.latestGeneration('inbox-feed-refresh') ?? 0) > read.generation;
      if (
        (!invalidatedByMutation && !supersededByCursorRead) ||
        supersededByRefresh ||
        supersededByReconcileRead ||
        reconcilePendingRef.current
      ) {
        return false;
      }
      queueReconcileIntent();
      return true;
    };
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
      const feedCurrent = readOwnershipRef.current.isCurrent(read, 'inbox-feed');
      const supersededByCursorRead =
        (readOwnershipRef.current.latestGeneration('inbox-feed-cursor') ?? 0) > read.generation;
      const supersededByReconcileRead =
        (readOwnershipRef.current.latestGeneration('inbox-feed-reconcile') ?? 0) > read.generation;
      if (!feedCurrent || supersededByCursorRead || supersededByReconcileRead) {
        retryAfterInvalidation();
      } else {
        const snapshotsToRevalidate = acceptFeedRows(read, result.sessions, true);
        setInboxSessions((prev) => {
          const incoming = new Map(result.sessions.map((s) => [s.session_id, s]));
          const merged = prev.map((s) => incoming.get(s.session_id) ?? s);
          const have = new Set(prev.map((s) => s.session_id));
          for (const s of result.sessions) if (!have.has(s.session_id)) merged.push(s);
          merged.sort(byActivityDesc);
          return mergeTargetedSnapshots(merged);
        });
        for (const sessionId of snapshotsToRevalidate) reconcileSessionRef.current(sessionId);
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
        if (!overlap) applyNextCursor(result.next_cursor);
      }
      if (readOwnershipRef.current.isCurrent(read, 'inbox-unread')) {
        applyWholeUnreadRead(read, result.unread_by_session ?? {});
      }
    } catch (err) {
      if (!retryAfterInvalidation()) console.error('[inbox] reconcile failed', err);
    } finally {
      broadReadInFlightRef.current = false;
      flushFeedReadIntent();
    }
  }, [acceptFeedRows, api, applyNextCursor, applyWholeUnreadRead, flushFeedReadIntent, mergeTargetedSnapshots, queueReconcileIntent]);

  // Counts-only read for routes that render no feed. Same endpoint and the same
  // pagination-independent unread map as refresh()/reconcile(), with the row
  // window collapsed to the smallest the API accepts — it claims only the unread
  // resource, discards the row it is handed, and never touches the feed or the
  // cursor, so activating the feed later still starts from a real first page.
  // (The server clamps `limit` to at least 1; a true zero-row read would need an
  // API change, deliberately out of scope here.)
  const refreshUnread = useCallback(async function refreshUnread() {
    if (unreadReadInFlightRef.current) {
      unreadReadPendingRef.current = true;
      return;
    }
    unreadReadInFlightRef.current = true;
    // A mid-flight mutation invalidates this response, and unlike the feed path
    // nothing else is queued to replace it: the ``inbox.session.updated`` handler
    // merges one session's count and never claims an authoritative whole map, so
    // silently discarding would leave every other session's count — and the
    // favicon/PWA badge — missing until some later focus or reconnect. Re-read,
    // exactly as ``refresh`` does. A newer read of the same resource is a
    // different case: it owns the map now and will apply its own, so stepping
    // aside is correct and keeps this terminating.
    const staleReadNeedsReread = (read: WorkbenchSessionReadStamp) =>
      !readOwnershipRef.current.isMutationCurrent(read, 'inbox-unread') &&
      (readOwnershipRef.current.latestGeneration('inbox-unread') ?? 0) <= read.generation;
    try {
      do {
        unreadReadPendingRef.current = false;
        const read = readOwnershipRef.current.beginRead(['inbox-unread']);
        try {
          const result = await api.listInbox({
            platform: 'avibe',
            limit: 1,
            cache: false,
            handleError: false,
          });
          if (readOwnershipRef.current.isCurrent(read, 'inbox-unread')) {
            applyWholeUnreadRead(read, result.unread_by_session ?? {});
          } else if (staleReadNeedsReread(read)) {
            unreadReadPendingRef.current = true;
          }
        } catch (err) {
          if (staleReadNeedsReread(read)) unreadReadPendingRef.current = true;
          else console.error('[inbox] refreshUnread failed', err);
        }
      } while (unreadReadPendingRef.current);
    } finally {
      unreadReadInFlightRef.current = false;
    }
  }, [api, applyWholeUnreadRead]);

  // Same rule as the projects tree: a reconnect is revalidation and may wait for
  // a consumer, but an authorization change is invalidation and cannot. The feed's
  // resumption path merges rather than replaces — ``reconcile`` deliberately keeps
  // rows the response omitted — so rows loaded before the change would survive the
  // trip back, and a failed reconcile would keep them indefinitely. Drop them and
  // reset ``initialFetched`` so the next feed consumer takes the authoritative
  // page-one path instead.
  //
  // The unread map is deliberately NOT dropped here: the counts read that follows
  // replaces it whole, and blanking it first would clear a badge the push service
  // worker may legitimately own while this document was never authoritative (the
  // ``unreadLoaded`` invariant). A count is a number for a session id the user
  // already had; a feed row is its title and preview.
  const discardAuthorizedFeed = useCallback(() => {
    const cachedSessionIds = new Set([
      ...inboxSessionsRef.current.map((row) => row.session_id),
      ...targetedSnapshotsRef.current.keys(),
      ...targetedReadInFlightRef.current,
    ]);
    // Fence the reads already in flight: a response that left the server before
    // the change must not repopulate what we are dropping.
    readOwnershipRef.current.acceptMutation([
      'inbox-feed',
      ...[...cachedSessionIds].map((sessionId) => `inbox-session:${sessionId}`),
    ]);
    initialFetched.current = false;
    inboxSessionsRef.current = [];
    targetedSnapshotsRef.current.clear();
    targetedReadPendingRef.current.clear();
    setInboxSessions([]);
    applyNextCursor(null);
  }, [applyNextCursor]);

  // Targeted reconcile for one session (contract A6 foreground restore): fetch
  // that exact session by id and upsert its card, so a restored session
  // reappears even when its activity sorts past the windowed reconcile()'s
  // newest-N rows. The response still carries the pagination-independent
  // whole-account unread map, but this read owns only this session's count.
  // Never touches the cursor or replaces unrelated unread state.
  const reconcileSession = useCallback(
    async (sessionId: string) => {
      if (targetedReadInFlightRef.current.has(sessionId)) {
        targetedReadPendingRef.current.add(sessionId);
        return;
      }
      targetedReadInFlightRef.current.add(sessionId);
      try {
        while (true) {
          targetedReadPendingRef.current.delete(sessionId);
          const sessionResource = `inbox-session:${sessionId}`;
          const unreadResource = `inbox-unread-session:${sessionId}`;
          const read = readOwnershipRef.current.beginRead([sessionResource, unreadResource]);
          try {
            const result = await api.listInbox({
              platform: 'avibe',
              onlySession: sessionId,
              limit: 1,
              cache: false,
              handleError: false,
            });
            const supersededByPendingIntent = targetedReadPendingRef.current.has(sessionId);
            const row = result.sessions.find((s) => s.session_id === sessionId);
            if (
              !supersededByPendingIntent &&
              readOwnershipRef.current.isCurrent(read, sessionResource)
            ) {
              targetedSnapshotsRef.current.set(sessionId, { row: row ?? null });
              setInboxSessions((prev) =>
                row
                  ? upsertSession(prev, row)
                  : prev.filter((candidate) => candidate.session_id !== sessionId),
              );
            }
            const newerCommittedWholeUnread =
              committedWholeUnreadGenerationRef.current > read.generation;
            if (
              !supersededByPendingIntent &&
              readOwnershipRef.current.isCurrent(read, unreadResource) &&
              readOwnershipRef.current.isMutationCurrent(read, 'inbox-unread-all') &&
              !newerCommittedWholeUnread
            ) {
              applySessionUnread(sessionId, result.unread_by_session?.[sessionId] ?? 0);
            }
          } catch (err) {
            console.error('[inbox] reconcileSession failed', err);
          }
          if (!targetedReadPendingRef.current.has(sessionId)) return;
        }
      } finally {
        targetedReadInFlightRef.current.delete(sessionId);
      }
    },
    [api, applySessionUnread],
  );
  refreshRunnerRef.current = () => void refresh();
  reconcileRunnerRef.current = () => void reconcile();
  loadMoreRunnerRef.current = () => void loadMore();
  reconcileSessionRef.current = (sessionId) => void reconcileSession(sessionId);

  // The feed's first read waits for a consumer that renders it. ``feedActive`` is
  // state, so it flips one render after the activation and this effect is what
  // runs the read — both for a workbench document's first paint and for a later
  // /admin → /chat navigation, which finds ``initialFetched`` still false and
  // loads page one exactly as a direct load would.
  useEffect(() => {
    if (!feedActive) return;
    // First read loads page one; every later rerun reconciles the loaded window
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
  }, [feedActive, reconcile, refresh]);

  // The badges are shell-wide, so the unread map is loaded on every route — but on
  // a route with no feed consumer, only the map. This is the one place that read is
  // issued for demand (the resume listeners below issue it for revalidation), and
  // all three of its conditions are load-bearing:
  //
  //  * ``isFeedActive()`` read synchronously, because consumers activate in their
  //    own effects and React runs those before this one: the refcount is already up
  //    on a workbench document's first commit, so the counts read is skipped in
  //    favour of the feed read that supersedes it.
  //  * ``feedActive`` as a dependency, because the demand EDGE matters too. When the
  //    last feed consumer detaches, the gate in ``flushFeedReadIntent`` drops
  //    whatever feed read was queued — and a dropped refresh/reconcile was also
  //    going to deliver the whole-account map. Nothing else would notice, because
  //    the refcount function has stable identity by design.
  //  * ``wholeUnreadOwed``, because that edge must not cost a request when it is not
  //    owed: leaving a healthy workbench document re-runs this effect with a map
  //    that is already whole, and re-reading it there would put back exactly the
  //    per-navigation traffic this provider exists to avoid.
  useEffect(() => {
    if (isFeedActive()) return;
    if (!wholeUnreadOwed) return;
    void refreshUnread();
  }, [feedActive, isFeedActive, refreshUnread, wholeUnreadOwed]);

  useEffect(() => {
    const disconnect = api.connectWorkbenchEvents({
      onAuthorizationChanged: () => {
        // A permission change invalidates whatever is loaded; re-read exactly
        // what this route reads — and, when no route reads the feed, still drop
        // the rows nobody is watching rather than keeping them for later.
        //
        // No committed whole map describes the new scope, so owe one rather than
        // naming the read that pays it: with a feed consumer the refresh below
        // carries it, without one the counts effect runs on this very state
        // change, and if the refresh is later dropped or fails the debt is still
        // outstanding when the last consumer leaves.
        setWholeUnreadOwed(true);
        if (isFeedActive()) {
          void refresh();
          return;
        }
        discardAuthorizedFeed();
      },
      onInboxSessionUpdated: (row) => {
        targetedSnapshotsRef.current.delete(row.session_id);
        acceptSessionMutation(row.session_id);
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
        acceptUnreadMutation();
        if (data?.unread_by_session) {
          applyUnreadMap(data.unread_by_session);
          return;
        }
        // The counts changed and the event did not say how. The mutation above
        // already invalidated every read in flight, so nothing is left to adopt a
        // map: owe one.
        setWholeUnreadOwed(true);
      },
      onSessionActivity: (data) => {
        // Contract A6: react to visibility/scope changes carried on the event.
        // background ⇒ drop the card (like an archive); foreground ⇒ fetch that
        // exact session and upsert its card so it reappears even if it sorts past
        // the reconcile window; no visibility ⇒ no-op except an explicit archive.
        // See lib/inboxActivity.
        const action = sessionActivityInboxAction(data);
        if (action === 'reconcile') {
          targetedSnapshotsRef.current.delete(data.session_id);
          acceptSessionMutation(data.session_id);
          void reconcileSession(data.session_id);
          return;
        }
        if (action === 'ignore') return;
        targetedSnapshotsRef.current.delete(data.session_id);
        acceptSessionMutation(data.session_id);
        // action === 'drop': remove the card + its unread live, instead of
        // waiting for the next reconnect/refresh to filter it.
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
  }, [
    acceptSessionMutation,
    acceptUnreadMutation,
    api,
    applyUnreadMap,
    discardAuthorizedFeed,
    isFeedActive,
    reconcileSession,
    refresh,
  ]);

  // Recover after the OS suspended us. A backgrounded mobile PWA has its page
  // frozen and its SSE socket dropped, and the broker never replays the gap;
  // iOS can leave EventSource in a zombie OPEN state without onerror. ApiContext
  // reopens the shared stream on visibility, online, and focus; independently
  // reconcile the durable feed here so missed events never gate data freshness.
  useEffect(() => {
    const resync = () => {
      if (document.visibilityState !== 'visible') return;
      if (isFeedActive()) void reconcile();
      else void refreshUnread();
    };
    document.addEventListener('visibilitychange', resync);
    window.addEventListener('online', resync);
    window.addEventListener('focus', resync);
    return () => {
      document.removeEventListener('visibilitychange', resync);
      window.removeEventListener('online', resync);
      window.removeEventListener('focus', resync);
    };
  }, [isFeedActive, reconcile, refreshUnread]);

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

  // Browser tabs have no Badging API. Keep their favicon useful while the
  // Inbox map is authoritative, and restore the original icon after reading.
  useEffect(() => {
    if (!unreadLoaded) return;
    syncFaviconBadge(totalUnread);
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
      activateFeed,
    }),
    [
      activateFeed,
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
