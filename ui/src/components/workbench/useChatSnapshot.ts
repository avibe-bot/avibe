import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';

import type {
  VibeAgentBrief,
  WorkbenchMessage,
  WorkbenchSession,
  WorkbenchSessionBootstrap,
} from '../../context/ApiContext';
import {
  chatSnapshotFromBootstrap,
  chatSnapshotOwner,
  chatSnapshotWindow,
  forgetChatSnapshot,
  isChatSnapshotOwnerCurrent,
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
//    commits first) removes every painted row before it lands. Rows that came
//    over the stream meanwhile were never painted, so they stay.
//  * The painted Session row, ``can_chat``, and Agents only decide what renders
//    until the bootstrap answers. Nothing here enables an action.
//  * A different reader, or anything that clears the store (an authorization
//    change, sign-out), drops what was painted at once and stops all storing
//    until the next bootstrap answers.
//  * Store only what the server answered for the reader it answered: the
//    bootstrap's row, Agents and ``can_chat``, plus the stored rows of the live
//    transcript window, a second after it settles, when leaving the chat, and
//    when the page is hidden.

export const CHAT_SNAPSHOT_WRITE_DELAY_MS = 1000;

/** What a painted snapshot renders until the bootstrap answers. */
export type ChatSnapshotView = {
  sessionId: string;
  principal: string;
  session: WorkbenchSession;
  canChat: boolean;
  agents: VibeAgentBrief[];
  defaultAgentName: string | null;
};

export type ChatSnapshotBootstrapRead = {
  /** The bootstrap answered for the open chat and its state is installed. */
  commit: (bootstrap: WorkbenchSessionBootstrap) => void;
  /** The bootstrap failed for the open chat. */
  fail: (error: unknown) => void;
};

type Owned = { owner: ChatSnapshotOwner; snapshot: ChatSnapshot; added: boolean };
type Pending = Owned;

type Options = {
  sessionId: string | undefined;
  /** Put the cached rows and cursor on screen, onto empty state only. Returns
   *  false to leave this chat unpainted. */
  paint: (snapshot: ChatSnapshot) => boolean;
  /** Remove the painted rows and the painted older-page cursor. */
  unpaint: (ids: ReadonlySet<string>) => void;
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

export function useChatSnapshot({ sessionId, paint, unpaint, messages, olderCursor, historicalWindow }: Options) {
  const paintRef = useRef(paint);
  const unpaintRef = useRef(unpaint);
  const routeRef = useRef(sessionId);
  useLayoutEffect(() => {
    paintRef.current = paint;
    unpaintRef.current = unpaint;
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
  const paintedRef = useRef<{ principal: string; ids: Set<string> } | null>(null);
  // The chat had no usable record when it opened, so its first write adds one.
  const missingRef = useRef(true);

  useEffect(() => {
    liveRef.current = false;
    paintedRef.current = null;
    missingRef.current = true;
  }, [sessionId]);

  const release = useCallback(() => {
    liveRef.current = true;
    const painted = paintedRef.current;
    paintedRef.current = null;
    if (painted) unpaintRef.current(painted.ids);
  }, []);

  useEffect(() => (
    subscribeLocalSnapshotPrincipal((next) => {
      const painted = paintedRef.current;
      if (painted && painted.principal !== next) {
        paintedRef.current = null;
        unpaintRef.current(painted.ids);
      }
      setView((current) => (current && current.principal !== next ? null : current));
      setOwned((current) => (current && current.owner.principal !== next ? null : current));
    })
  ), []);

  useEffect(() => subscribeLocalSnapshotEpoch(() => {
    const painted = paintedRef.current;
    paintedRef.current = null;
    if (painted) unpaintRef.current(painted.ids);
    setView(null);
    setOwned(null);
  }), []);

  useEffect(() => {
    if (!sessionId || !principal) return undefined;
    let cancelled = false;
    void readChatSnapshot(sessionId).then((snapshot) => {
      if (cancelled) return;
      if (snapshot) missingRef.current = false;
      if (!snapshot || liveRef.current || getLocalSnapshotPrincipal() !== principal) return;
      if (!paintRef.current(snapshot)) return;
      paintedRef.current = { principal, ids: new Set(snapshot.messages.map((row) => row.id)) };
      setView({
        sessionId,
        principal,
        session: snapshot.session,
        canChat: snapshot.can_chat,
        agents: snapshot.agents,
        defaultAgentName: snapshot.default_agent_name,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, principal]);

  const beginBootstrap = useCallback((requestSessionId: string): ChatSnapshotBootstrapRead => {
    // Taken before the request: a reader change or a store clear while it is
    // out means its answer is not this reader's to store.
    const owner = chatSnapshotOwner(requestSessionId);
    return {
      commit: (bootstrap) => {
        if (routeRef.current !== requestSessionId) return;
        liveRef.current = true;
        setView(null);
        setOwned(owner && isChatSnapshotOwnerCurrent(owner)
          ? { owner, snapshot: chatSnapshotFromBootstrap(bootstrap), added: missingRef.current }
          : null);
      },
      fail: (error) => {
        if (routeRef.current !== requestSessionId) return;
        release();
        setView(null);
        const status = errorStatus(error);
        if (status === 403 || status === 404) {
          setOwned(null);
          void forgetChatSnapshot(requestSessionId);
        }
      },
    };
  }, [release]);

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
