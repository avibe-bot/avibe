import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  useApi,
  type SessionRuntimeState,
  type WorkbenchMessage,
  type WorkbenchSession,
  type WorkbenchSessionReadResult,
} from '@/context/ApiContext';
import { isSessionReadOnly } from '@/components/workbench/sessionArchived';
import { isTranscriptMessage } from '@/lib/chatMessageTypes';
import { onPageReactivated } from '@/lib/pageActivity';

import { FencedSource } from './fencedSource';

export const PET_TAIL_LIMIT = 30;
// Same cadences as the chat page: reconcile a dropped turn.end without ever
// clearing a live turn on a timer, faster while background work is present.
const WORKING_RECONCILE_INTERVAL_MS = 60 * 1000;
const ACTIVITY_RECONCILE_INTERVAL_MS = 10 * 1000;
// A just-sent turn is not in the controller's in-flight map until dispatch
// lands, so the pet shows Running for this long after a local send.
export const SEND_GRACE_MS = 4000;

export type PetSessionData = {
  session: WorkbenchSession | null;
  messages: WorkbenchMessage[];
  /** Rows before `messages` may exist: the tail was trimmed, or no tail read
   *  has landed yet, so live rows alone say nothing about what came before. */
  hasOlder: boolean;
  turn: SessionRuntimeState | null;
  /** Running per the server, or a local send inside the registration grace. */
  running: boolean;
  /** Merge a row the pet itself just sent. */
  noteSent: (message: WorkbenchMessage | null) => void;
  /** Re-read the tail from the server. */
  refreshTail: () => void;
};

// `loaded`: a tail read has applied. Until then the rows are only what live
// events merged, so they cannot be read as a complete window.
type Tail = { messages: WorkbenchMessage[]; hasOlder: boolean; loaded: boolean };
const EMPTY_TAIL: Tail = { messages: [], hasOlder: false, loaded: false };
const TAIL_CAP = PET_TAIL_LIMIT * 2;

/**
 * Merge a live row into the tail. Only transcript rows are kept (process rows
 * such as tool calls stream in by the hundred and the pet never shows them),
 * and the tail is capped; trimming means it no longer starts at the session's
 * first row, so `hasOlder` turns true and unread rows past it are not marked.
 */
const mergeRow = (tail: Tail, row: WorkbenchMessage): Tail => {
  const index = tail.messages.findIndex((existing) => existing.id === row.id);
  if (index >= 0) {
    return { ...tail, messages: tail.messages.map((existing, at) => (at === index ? row : existing)) };
  }
  if (!isTranscriptMessage(row)) return tail;
  const messages = [...tail.messages, row];
  if (messages.length <= TAIL_CAP) return { ...tail, messages };
  return { ...tail, messages: messages.slice(-TAIL_CAP), hasOlder: true };
};

/**
 * Everything the pet reads about its bound session `S`, per the input-freshness
 * contract in docs/plans/2026-10-01-desktop-pet.md: an initial read, live
 * triggers, and gap fallbacks, with every read fenced by binding and order.
 *
 * `onInvalid(S)` fires when `S` turns out to be read-only or missing, so the
 * caller can clear the binding (compare-and-clear) and show the switcher.
 */
export function usePetSession(sessionId: string | null, onInvalid: (sessionId: string) => void): PetSessionData {
  const api = useApi();

  const [session, setSession] = useState<WorkbenchSession | null>(null);
  const [tail, setTail] = useState<Tail>(EMPTY_TAIL);
  const [turn, setTurn] = useState<SessionRuntimeState | null>(null);
  // The session a fresh read found missing or read-only.
  const [invalid, setInvalid] = useState<string | null>(null);
  // A local send stays Running for the registration grace.
  const [inGrace, setInGrace] = useState(false);
  const [sendCount, setSendCount] = useState(0);

  // Reset during render so a switch never paints the previous session's rows.
  const [displayed, setDisplayed] = useState(sessionId);
  if (displayed !== sessionId) {
    setDisplayed(sessionId);
    setSession(null);
    setTail(EMPTY_TAIL);
    setTurn(null);
    setInGrace(false);
    setInvalid(null);
  }

  const sources = useMemo(() => ({
    session: new FencedSource<string, WorkbenchSessionReadResult>({
      read: (key) => api.getSessionResult(key),
      apply: (key, { status, session: row }) => {
        // A binding is valid only while S is writable: missing, archived and
        // Runtime-owned sessions all clear it.
        // 403: this principal lost access to the session (or its project).
        if (status === 404 || status === 403 || (row && isSessionReadOnly(row))) {
          setInvalid(key);
          return;
        }
        if (row) setSession(row);
      },
    }),
    tail: new FencedSource<string, { messages: WorkbenchMessage[]; next_before_id?: string | null }>({
      read: (key) => api.listSessionMessages(key, { tail: true, cache: false, limit: PET_TAIL_LIMIT }),
      apply: (_key, value) => {
        setTail({ messages: value.messages, hasOlder: Boolean(value.next_before_id), loaded: true });
      },
    }),
    turn: new FencedSource<string, SessionRuntimeState>({
      read: (key) => api.getTurnState(key, { handleError: false }),
      apply: (_key, value) => {
        if (value.foreground !== 'unknown') setTurn(value);
      },
    }),
  }), [api]);

  const refreshAll = useCallback(() => {
    sources.session.refresh();
    sources.tail.refresh();
    sources.turn.refresh();
  }, [sources]);

  // Initial read on every binding change.
  useEffect(() => {
    sources.session.setKey(sessionId);
    sources.tail.setKey(sessionId);
    sources.turn.setKey(sessionId);
    if (sessionId) refreshAll();
  }, [sessionId, sources, refreshAll]);

  // Report an invalid binding once, and only while it is still the binding.
  useEffect(() => {
    if (invalid && invalid === sessionId) onInvalid(invalid);
  }, [invalid, sessionId, onInvalid]);

  // Live triggers and the reconnect gap fallback.
  useEffect(() => {
    if (!sessionId) return undefined;
    const mine = (id: string | null | undefined) => id === sessionId;
    return api.connectWorkbenchEvents({
      onConnected: refreshAll,
      onMessageNew: (row) => {
        if (!mine(row.session_id)) return;
        sources.tail.noteLiveMerge();
        setTail((current) => mergeRow(current, row));
      },
      onMessageUpdated: (row) => {
        if (!mine(row.session_id)) return;
        sources.tail.noteLiveMerge();
        setTail((current) => ({
          ...current,
          messages: current.messages.map((existing) => (existing.id === row.id ? row : existing)),
        }));
      },
      onSessionStatus: ({ session_id, agent_status }) => {
        if (!mine(session_id)) return;
        sources.session.noteLiveMerge();
        setSession((current) => (current ? { ...current, agent_status } : current));
      },
      onSessionActivity: ({ session_id }) => {
        if (mine(session_id)) sources.session.refresh();
      },
      onTurnStart: ({ session_id }) => {
        if (mine(session_id)) sources.turn.refresh();
      },
      onTurnEnd: ({ session_id }) => {
        if (!mine(session_id)) return;
        // An authoritative end: the post-send grace only guards idle reads that
        // race turn registration, so it ends here too.
        setInGrace(false);
        sources.turn.refresh();
      },
      // Access changed: drop everything read under the old authorization at
      // once, then re-read (a refresh also drops any read in flight); a lost
      // session reads 403 and clears the binding.
      onAuthorizationChanged: () => {
        setSession(null);
        setTail(EMPTY_TAIL);
        setTurn(null);
        refreshAll();
      },
      onQueueUpdated: ({ session_id }) => {
        if (mine(session_id)) sources.turn.refresh();
      },
      // Background activities are owned by S through definitions and callback
      // runs, which neither event names: re-read on every one, coalesced.
      onRunsUpdated: () => sources.turn.refresh(),
      onDefinitionsUpdated: () => sources.turn.refresh(),
    });
  }, [api, sessionId, sources, refreshAll]);

  // The window coming back is a gap too.
  useEffect(() => {
    if (!sessionId) return undefined;
    return onPageReactivated(refreshAll);
  }, [sessionId, refreshAll]);

  const serverRunning = turn?.foreground === 'running' || turn?.in_flight === true;
  const working = serverRunning || inGrace;
  const hasBackground = (turn?.background_activities.length ?? 0) > 0;

  // Re-read once the send grace expires, so a quick turn whose turn.end was
  // missed still settles.
  useEffect(() => {
    if (sendCount === 0) return undefined;
    const timer = window.setTimeout(() => {
      setInGrace(false);
      sources.turn.refresh();
    }, SEND_GRACE_MS);
    return () => window.clearTimeout(timer);
  }, [sendCount, sources]);

  // Interval reconcile only while work is present and the window is visible.
  useEffect(() => {
    if (!sessionId || (!working && !hasBackground)) return undefined;
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') sources.turn.refresh();
    }, hasBackground ? ACTIVITY_RECONCILE_INTERVAL_MS : WORKING_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [sessionId, working, hasBackground, sources]);

  const noteSent = useCallback((row: WorkbenchMessage | null) => {
    setInGrace(true);
    setSendCount((count) => count + 1);
    if (row && row.session_id === sessionId && row.id) {
      sources.tail.noteLiveMerge();
      setTail((current) => mergeRow(current, row));
    }
    sources.tail.refresh();
    sources.turn.refresh();
  }, [sources, sessionId]);

  const refreshTail = useCallback(() => sources.tail.refresh(), [sources]);

  return {
    session,
    messages: tail.messages,
    hasOlder: tail.hasOlder || !tail.loaded,
    turn,
    running: working,
    noteSent,
    refreshTail,
  };
}
