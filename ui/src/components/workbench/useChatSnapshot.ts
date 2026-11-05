import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';

import type { WorkbenchMessage, WorkbenchSessionBootstrap } from '../../context/ApiContext';
import {
  beginChatSnapshotRequest,
  chatSnapshotFromBootstrap,
  chatSnapshotOwnerForAnswer,
  chatSnapshotWindow,
  forgetChatSnapshot,
  readChatSnapshot,
  writeChatSnapshot,
  type ChatSnapshot,
  type ChatSnapshotOwner,
} from '../../lib/chatSnapshots';
import {
  getLocalSnapshotPrincipal,
  subscribeLocalSnapshotEpoch,
  subscribeLocalSnapshotPrincipal,
} from '../../lib/localSnapshots';
import { loadKnownVoiceStatus } from '../../lib/voiceAvailability';

// The conversation snapshot for ChatPage (docs/plans/web-ui-local-cache.md,
// Phase 2): paint the last known window of the opened chat while its bootstrap
// is out, hand the screen to the first live answer, and keep the record current
// from what the server sends afterwards.
//
// ChatPage owns the transcript state; this hook owns when a snapshot may touch
// it. The rules:
//
//  * Paint once per opened chat, onto empty state, before anything live landed
//    for it, and for the reader whose record it is.
//  * The first live transcript window (the bootstrap, or a recovery read that
//    commits first) removes every painted row before it lands. Rows are
//    tracked by version, not id: a row the stream delivered or updated
//    meanwhile is live, so it stays, and the live window merges onto it.
//  * ChatPage seeds the page's own state from the snapshot (the Session row,
//    ``can_chat``, Agents), so the chat renders fully formed and usable from
//    the cache; the live answer may only narrow it, and the server enforces
//    every action.
//  * A different reader, anything that clears the store (an authorization
//    change, sign-out), or a failed bootstrap drops what was painted and seeded
//    at once; the first two also stop all storing until the next bootstrap
//    answers.
//  * Store only what the server answered for the reader it answered: the
//    bootstrap's row, Agents and ``can_chat``, plus the stored rows of the live
//    transcript window, a second after it settles, when leaving the chat, and
//    when the page is hidden.

const CHAT_SNAPSHOT_WRITE_DELAY_MS = 1000;

/** A chat painted from the snapshot, until its bootstrap answers. */
type ChatSnapshotView = { sessionId: string; principal: string };

type ChatSnapshotBootstrapRead = {
  /** The bootstrap answered for the open chat and its state is installed. */
  commit: (bootstrap: WorkbenchSessionBootstrap) => void;
  /** The bootstrap failed for the open chat. */
  fail: (error: unknown) => void;
};

type Owned = { owner: ChatSnapshotOwner; snapshot: ChatSnapshot; added: boolean };
type Pending = Owned;

type Options = {
  sessionId: string | undefined;
  /** Seed the page from the snapshot: rows and cursor onto an empty transcript,
   *  and the chrome state the bootstrap would supply. Returns false to leave
   *  this chat unpainted. */
  paint: (snapshot: ChatSnapshot) => boolean;
  /** Remove the painted rows (these exact row objects; a row the stream has
   *  since replaced is live and stays) and the painted older-page cursor. A
   *  live transcript window is about to land. */
  unpaint: (rows: ReadonlySet<WorkbenchMessage>) => void;
  /** Withdraw everything seeded from the snapshot: the painted rows (if still
   *  on screen) and the chrome state. The cache no longer speaks for this page. */
  drop: (rows: ReadonlySet<WorkbenchMessage> | null) => void;
  /** The live transcript window and its cursor, for write-back. */
  messages: WorkbenchMessage[];
  olderCursor: string | null;
  /** True while the window does not reach the live tail; nothing is stored then. */
  historicalWindow: boolean;
};

const errorStatus = (error: unknown): number | null => {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === 'number' ? status : null;
};

export function useChatSnapshot({ sessionId, paint, unpaint, drop, messages, olderCursor, historicalWindow }: Options) {
  const paintRef = useRef(paint);
  const unpaintRef = useRef(unpaint);
  const dropRef = useRef(drop);
  const routeRef = useRef(sessionId);
  useLayoutEffect(() => {
    paintRef.current = paint;
    unpaintRef.current = unpaint;
    dropRef.current = drop;
    routeRef.current = sessionId;
  });

  const principal = useSyncExternalStore(subscribeLocalSnapshotPrincipal, getLocalSnapshotPrincipal);
  const [view, setView] = useState<ChatSnapshotView | null>(null);
  const [owned, setOwned] = useState<Owned | null>(null);
  // Another chat opened: what was painted or owned for the last one is not this
  // one's (the reset React documents for state derived from a changed prop).
  const [route, setRoute] = useState(sessionId);
  if (route !== sessionId) {
    setRoute(sessionId);
    setView(null);
    setOwned(null);
  }
  // Per opened chat: something live landed for it, so nothing is painted now.
  const liveRef = useRef(false);
  // Rows on screen from the snapshot that no live window has replaced yet.
  const paintedRef = useRef<{ principal: string; rows: Set<WorkbenchMessage> } | null>(null);
  // The reader the open chat was seeded for, until its bootstrap answers.
  const seededRef = useRef<string | null>(null);
  // The chat had no usable record when it opened, so its first write adds one.
  const missingRef = useRef(true);

  useEffect(() => {
    liveRef.current = false;
    paintedRef.current = null;
    seededRef.current = null;
    missingRef.current = true;
  }, [sessionId]);

  // The cache stops speaking for the page: withdraw what it seeded.
  const withdraw = useCallback(() => {
    const painted = paintedRef.current;
    const seeded = seededRef.current;
    paintedRef.current = null;
    seededRef.current = null;
    if (seeded !== null) dropRef.current(painted?.rows ?? null);
    else if (painted) unpaintRef.current(painted.rows);
    setView(null);
  }, []);

  const release = useCallback(() => {
    liveRef.current = true;
    const painted = paintedRef.current;
    paintedRef.current = null;
    if (painted) unpaintRef.current(painted.rows);
  }, []);

  useEffect(() => (
    subscribeLocalSnapshotPrincipal((next) => {
      const painted = paintedRef.current;
      const seeded = seededRef.current;
      if ((seeded !== null && seeded !== next) || (painted && painted.principal !== next)) withdraw();
      setOwned((current) => (current && current.owner.principal !== next ? null : current));
    })
  ), [withdraw]);

  useEffect(() => subscribeLocalSnapshotEpoch(() => {
    withdraw();
    setOwned(null);
  }), [withdraw]);

  useEffect(() => {
    if (!sessionId || !principal) return undefined;
    let cancelled = false;
    // The composer renders its voice control from the last known status, so
    // that status is in memory before the chat paints (lib/voiceAvailability).
    void Promise.all([readChatSnapshot(sessionId), loadKnownVoiceStatus()]).then(([snapshot]) => {
      if (cancelled) return;
      if (snapshot) missingRef.current = false;
      if (!snapshot || liveRef.current || getLocalSnapshotPrincipal() !== principal) return;
      if (!paintRef.current(snapshot)) return;
      paintedRef.current = { principal, rows: new Set(snapshot.messages) };
      seededRef.current = principal;
      setView({ sessionId, principal });
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, principal]);

  const beginBootstrap = useCallback((requestSessionId: string): ChatSnapshotBootstrapRead => {
    // Noted before the request: a reader change, a store clear, or a forget
    // while it is out means its answer is not this reader's to store.
    const request = beginChatSnapshotRequest(requestSessionId);
    return {
      commit: (bootstrap) => {
        if (routeRef.current !== requestSessionId) return;
        liveRef.current = true;
        // The bootstrap's state replaced every seeded value; nothing is left
        // for the cache to withdraw.
        seededRef.current = null;
        setView(null);
        const owner = chatSnapshotOwnerForAnswer(request);
        setOwned(owner ? { owner, snapshot: chatSnapshotFromBootstrap(bootstrap), added: missingRef.current } : null);
      },
      fail: (error) => {
        if (routeRef.current !== requestSessionId) return;
        liveRef.current = true;
        withdraw();
        const status = errorStatus(error);
        if (status === 403 || status === 404) {
          setOwned(null);
          void forgetChatSnapshot(requestSessionId);
        }
      },
    };
  }, [withdraw]);

  const pendingRef = useRef<Pending | null>(null);
  const timerRef = useRef<number | null>(null);
  const prunedForRef = useRef<ChatSnapshotOwner | null>(null);
  const flush = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const pending = pendingRef.current;
    pendingRef.current = null;
    if (!pending) return;
    const added = pending.added && prunedForRef.current !== pending.owner;
    if (added) prunedForRef.current = pending.owner;
    void writeChatSnapshot(pending.owner, pending.snapshot, { added });
  }, []);

  useEffect(() => {
    if (!owned || owned.owner.sessionId !== sessionId || historicalWindow || paintedRef.current) return;
    pendingRef.current = {
      ...owned,
      snapshot: { ...owned.snapshot, ...chatSnapshotWindow(messages, olderCursor) },
    };
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(flush, CHAT_SNAPSHOT_WRITE_DELAY_MS);
  }, [owned, sessionId, messages, olderCursor, historicalWindow, flush]);

  // Leaving the chat (or the page) writes what is owed at once.
  useEffect(() => flush, [sessionId, flush]);
  useEffect(() => {
    const onHidden = () => {
      if (document.visibilityState === 'hidden') flush();
    };
    document.addEventListener('visibilitychange', onHidden);
    return () => document.removeEventListener('visibilitychange', onHidden);
  }, [flush]);

  const isPainted = useCallback(() => paintedRef.current !== null, []);

  return {
    /** The painted snapshot for the open chat while its bootstrap is out. */
    view: view && view.sessionId === sessionId ? view : null,
    beginBootstrap,
    /** A live transcript window is about to commit for the open chat. */
    release,
    /** Painted rows are on screen; older paging waits for the live window. */
    isPainted,
  };
}
