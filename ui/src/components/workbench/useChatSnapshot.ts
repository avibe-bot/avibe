import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { knownChatSnapshot, readChatSnapshot, type ChatSnapshot } from '../../lib/chatSnapshots';
import {
  getLocalSnapshotPrincipal,
  subscribeLocalSnapshotEpoch,
  subscribeLocalSnapshotPrincipal,
} from '../../lib/localSnapshots';
import { loadKnownVoiceStatus } from '../../lib/voiceAvailability';
import type { ChatListSeed } from './chatListSeed';

// The conversation snapshot for ChatPage (docs/plans/local-sync-store.md
// section 4.3): paint the last known window of the opened chat while its
// bootstrap is out, and hand the screen to the first live answer. What it
// paints comes from the local message store (lib/chatSnapshots), which the API
// layer keeps current from every answer and streamed row, so this hook stores
// nothing itself.
//
// ChatPage owns the transcript state; this hook owns when a snapshot may touch
// it. The rules:
//
//  * Paint once per opened chat, onto empty state, before anything live landed
//    for it, and for the reader whose record it is. A record this document
//    holds in memory paints in the commit that opens the chat (``paintKnown``,
//    which ChatPage calls right after it resets for the route); otherwise the
//    stored record paints when IndexedDB answers.
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
//    at once.

/** A chat painted from the snapshot, until its bootstrap answers. ``partial``:
 *  painted from what the lists say (its row, perhaps its newest message), so the
 *  rest of its transcript is still to come. */
type ChatSnapshotView = { sessionId: string; principal: string; partial: boolean };

type ChatSnapshotBootstrapRead = {
  /** The bootstrap answered for the open chat and its state is installed. */
  commit: () => void;
  /** The bootstrap failed for the open chat. */
  fail: () => void;
};

type Options = {
  sessionId: string | undefined;
  /** Seed the page from the snapshot: rows and cursor onto an empty transcript,
   *  and the chrome state the bootstrap would supply. ``partial``: built from
   *  the lists, so only its row and rows are known. Returns false to leave this
   *  chat unpainted. */
  paint: (snapshot: ChatSnapshot, partial: boolean) => boolean;
  /** What the lists the chat was opened from say about it, for the current
   *  reader, or null (chatListSeed). */
  knownFromLists: (sessionId: string) => ChatListSeed | null;
  /** Remove the painted rows (these exact row objects; a row the stream has
   *  since replaced is live and stays) and the painted older-page cursor. A
   *  live transcript window is about to land. */
  unpaint: (rows: ReadonlySet<WorkbenchMessage>) => void;
  /** Withdraw everything seeded from the snapshot: the painted rows (if still
   *  on screen) and the chrome state. The cache no longer speaks for this page. */
  drop: (rows: ReadonlySet<WorkbenchMessage> | null) => void;
};

const timeOf = (iso: string | null | undefined): number => {
  const ms = iso ? Date.parse(iso) : Number.NaN;
  return Number.isNaN(ms) ? 0 : ms;
};

/** Whether a stored window already holds what the lists know: nothing newer
 *  than its newest row. */
const coversSeed = (snapshot: ChatSnapshot, latestAt: string | null): boolean =>
  latestAt === null || timeOf(snapshot.messages[snapshot.messages.length - 1]?.created_at) >= timeOf(latestAt);

/** The page state a list seed paints: its row, and its newest message if known. */
const seedSnapshot = (seed: ChatListSeed): ChatSnapshot => ({
  session: seed.session,
  messages: seed.latest ? [seed.latest] : [],
  next_before_id: null,
  agents: [],
  default_agent_name: null,
  can_chat: false,
  chat_message_font_size: null,
});

export function useChatSnapshot({ sessionId, paint, knownFromLists, unpaint, drop }: Options) {
  const paintRef = useRef(paint);
  const knownFromListsRef = useRef(knownFromLists);
  const unpaintRef = useRef(unpaint);
  const dropRef = useRef(drop);
  const routeRef = useRef(sessionId);
  useLayoutEffect(() => {
    paintRef.current = paint;
    knownFromListsRef.current = knownFromLists;
    unpaintRef.current = unpaint;
    dropRef.current = drop;
    routeRef.current = sessionId;
  });

  const principal = useSyncExternalStore(subscribeLocalSnapshotPrincipal, getLocalSnapshotPrincipal);
  const [view, setView] = useState<ChatSnapshotView | null>(null);
  // Another chat opened: what was painted for the last one is not this one's
  // (the reset React documents for state derived from a changed prop).
  const [route, setRoute] = useState(sessionId);
  if (route !== sessionId) {
    setRoute(sessionId);
    setView(null);
  }
  // Per opened chat: something live landed for it, so nothing is painted now.
  const liveRef = useRef(false);
  // Rows on screen from the snapshot that no live window has replaced yet.
  const paintedRef = useRef<{ principal: string; rows: Set<WorkbenchMessage> } | null>(null);
  // The reader the open chat was seeded for, until its bootstrap answers.
  const seededRef = useRef<string | null>(null);
  // Painted from the lists: the time of the newest message they knew, if any. A
  // stored window as recent replaces it when IndexedDB answers.
  const partialRef = useRef<{ latestAt: string | null } | null>(null);

  // A layout effect, so it runs before the page's reset and its ``paintKnown``.
  useLayoutEffect(() => {
    liveRef.current = false;
    paintedRef.current = null;
    seededRef.current = null;
    partialRef.current = null;
  }, [sessionId]);

  const paintSnapshot = useCallback((snapshot: ChatSnapshot, reader: string, partial = false) => {
    if (!paintRef.current(snapshot, partial)) return;
    paintedRef.current = { principal: reader, rows: new Set(snapshot.messages) };
    seededRef.current = reader;
    partialRef.current = partial ? { latestAt: snapshot.messages[snapshot.messages.length - 1]?.created_at ?? null } : null;
    setView({ sessionId: snapshot.session.id, principal: reader, partial });
  }, []);

  // Paint what is known of the open chat, at once: the record this document
  // holds, or, without one as recent as what the lists know, the lists' seed (a
  // skeleton fills the rest of the transcript). Called by the page at the end of
  // the layout effect that resets it for a route, onto the state it just
  // emptied, so the first frame the browser paints is the chat, not a loading
  // state.
  const paintKnown = useCallback(() => {
    const route = routeRef.current;
    const reader = getLocalSnapshotPrincipal();
    if (!route || !reader) return;
    const snapshot = knownChatSnapshot(route);
    const seed = knownFromListsRef.current(route);
    if (snapshot && coversSeed(snapshot, seed?.latest?.created_at ?? null)) paintSnapshot(snapshot, reader);
    else if (seed) paintSnapshot(seedSnapshot(seed), reader, true);
  }, [paintSnapshot]);

  // The cache stops speaking for the page: withdraw what it seeded.
  const withdraw = useCallback(() => {
    const painted = paintedRef.current;
    const seeded = seededRef.current;
    paintedRef.current = null;
    seededRef.current = null;
    partialRef.current = null;
    if (seeded !== null) dropRef.current(painted?.rows ?? null);
    else if (painted) unpaintRef.current(painted.rows);
    setView(null);
  }, []);

  const release = useCallback(() => {
    liveRef.current = true;
    partialRef.current = null;
    const painted = paintedRef.current;
    paintedRef.current = null;
    if (painted) unpaintRef.current(painted.rows);
  }, []);

  useEffect(() => (
    subscribeLocalSnapshotPrincipal((next) => {
      const painted = paintedRef.current;
      const seeded = seededRef.current;
      if ((seeded !== null && seeded !== next) || (painted && painted.principal !== next)) withdraw();
    })
  ), [withdraw]);

  useEffect(() => subscribeLocalSnapshotEpoch(withdraw), [withdraw]);

  useEffect(() => {
    if (!sessionId || !principal) return undefined;
    let cancelled = false;
    // The composer renders its voice control from the last known status, so
    // that status is in memory before the chat paints (lib/voiceAvailability).
    void Promise.all([readChatSnapshot(sessionId), loadKnownVoiceStatus()]).then(([snapshot]) => {
      if (cancelled) return;
      if (!snapshot || liveRef.current || getLocalSnapshotPrincipal() !== principal) return;
      // A full window already on screen stays. One painted from the lists gives
      // way to a stored window that holds what they knew.
      const partial = partialRef.current;
      if (paintedRef.current && !partial) return;
      if (partial) {
        if (!coversSeed(snapshot, partial.latestAt)) return;
        const painted = paintedRef.current;
        paintedRef.current = null;
        partialRef.current = null;
        if (painted) unpaintRef.current(painted.rows);
      }
      paintSnapshot(snapshot, principal);
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, principal, paintSnapshot]);

  const beginBootstrap = useCallback((requestSessionId: string): ChatSnapshotBootstrapRead => ({
    commit: () => {
      if (routeRef.current !== requestSessionId) return;
      liveRef.current = true;
      // The bootstrap's state replaced every seeded value; nothing is left
      // for the cache to withdraw.
      seededRef.current = null;
      setView(null);
    },
    fail: () => {
      if (routeRef.current !== requestSessionId) return;
      liveRef.current = true;
      withdraw();
    },
  }), [withdraw]);

  const isPainted = useCallback(() => paintedRef.current !== null, []);

  // Rows the page added from the store while its bootstrap could not answer
  // (older history read offline): they go with the painted window when it lands.
  const paintMore = useCallback((rows: readonly WorkbenchMessage[]) => {
    const painted = paintedRef.current;
    if (!painted) return;
    for (const row of rows) painted.rows.add(row);
  }, []);

  return {
    /** The painted snapshot for the open chat while its bootstrap is out. */
    view: view && view.sessionId === sessionId ? view : null,
    beginBootstrap,
    /** A live transcript window is about to commit for the open chat. */
    release,
    /** Painted rows are on screen; older paging waits for the live window. */
    isPainted,
    /** Count ``rows`` as painted too: they leave when the live window lands. */
    paintMore,
    /** Paint the record held in memory for the open chat, if any: the page
     *  calls it in the layout effect that resets it for the route. */
    paintKnown,
  };
}
