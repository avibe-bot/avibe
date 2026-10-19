import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

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
import { useLatestRef } from '@/lib/useLatestRef';

import { FencedSource } from './fencedSource';
import { petShell } from './petShell';

export type PetSurfaceEpoch = {
  binding: string | null;
  authorizationGeneration: number;
  isCurrent: () => boolean;
};

export const PET_TAIL_LIMIT = 30;
// Same cadences as the chat page: reconcile a dropped turn.end without ever
// clearing a live turn on a timer, faster while background work is present.
const WORKING_RECONCILE_INTERVAL_MS = 60 * 1000;
const ACTIVITY_RECONCILE_INTERVAL_MS = 10 * 1000;
// A just-sent turn is not in the controller's in-flight map until dispatch
// lands, so the pet shows Running for this long after a local send.
export const SEND_GRACE_MS = 4000;

export type PetSessionData = {
  epoch: PetSurfaceEpoch;
  session: WorkbenchSession | null;
  /** Project-level chat grant from GET session; null until a read lands. */
  canChat: boolean | null;
  messages: WorkbenchMessage[];
  /** Rows before `messages` may exist: the tail was trimmed, or no tail read
   *  has landed yet, so live rows alone say nothing about what came before. */
  hasOlder: boolean;
  tailLoaded: boolean;
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
 * Main is resolved through the owner-only endpoint; native bindings are ignored.
 */
export function usePetSession(sessionId: string | null): PetSessionData {
  const api = useApi();
  const [authorizationGeneration, setAuthorizationGeneration] = useState(0);
  const authorizationNow = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const identity = useMemo(() => ({ binding: sessionId, authorizationGeneration }), [sessionId, authorizationGeneration]);
  const currentIdentity = useLatestRef(identity);
  const epoch = useMemo<PetSurfaceEpoch>(() => {
    const isAuthorizationCurrent = () => mounted.current && authorizationNow.current === identity.authorizationGeneration;
    return {
      ...identity,
      isCurrent: () => isAuthorizationCurrent() && currentIdentity.current === identity
        && (petShell.currentBinding() ?? null) === identity.binding,
    };
  }, [identity, currentIdentity]);

  const [session, setSession] = useState<WorkbenchSession | null>(null);
  const [canChat, setCanChat] = useState<boolean | null>(null);
  const [tail, setTail] = useState<Tail>(EMPTY_TAIL);
  const [turn, setTurn] = useState<SessionRuntimeState | null>(null);
  // A local send stays Running for the registration grace.
  const [inGrace, setInGrace] = useState(false);
  const [sendCount, setSendCount] = useState(0);

  // Reset during render so a switch never paints the previous session's rows.
  const [displayed, setDisplayed] = useState(sessionId);
  if (displayed !== sessionId) {
    setDisplayed(sessionId);
    setTail(EMPTY_TAIL);
    setTurn(null);
    setInGrace(false);
  }

  const sources = useMemo(() => ({
    session: new FencedSource<number, WorkbenchSessionReadResult>({
      read: () => api.getMainSession(),
      apply: (_key, { status, session: row, capabilities }) => {
        const valid = status === 200 && row && !isSessionReadOnly(row) && capabilities?.can_chat === true;
        setSession(valid ? row : null);
        setCanChat(Boolean(valid));
        petShell.setMain(valid ? row.id : null);
      },
      fail: () => {
        setSession(null);
        setCanChat(false);
        petShell.setMain(null);
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

  // A live event can only patch rows a read has supplied. Until one has (the
  // first read failed, say, while the stream stayed up), the event is the
  // next chance to read instead.
  const sessionRef = useLatestRef(session);
  const tailLoadedRef = useLatestRef(tail.loaded);

  const refreshAll = useCallback(() => {
    sources.session.refresh();
    sources.tail.refresh();
    sources.turn.refresh();
  }, [sources]);

  // One fenced main lookup per mount, then event-driven recovery. No polling.
  useEffect(() => {
    sources.session.setKey(authorizationNow.current);
    sources.session.refresh();
    return () => sources.session.setKey(null);
  }, [sources]);

  // No transcript or runtime reads before the main endpoint grants access.
  const resolved = session?.id === sessionId && canChat === true;
  useEffect(() => {
    const key = resolved ? sessionId : null;
    sources.tail.setKey(key);
    sources.turn.setKey(key);
    if (key) { sources.tail.refresh(); sources.turn.refresh(); }
    return () => { sources.tail.setKey(null); sources.turn.setKey(null); };
  }, [sessionId, resolved, authorizationGeneration, sources]);

  // Live triggers and the reconnect gap fallback.
  useEffect(() => {
    const generation = authorizationNow.current;
    const mine = (id: string | null | undefined) => Boolean(generation === authorizationNow.current && resolved && sessionId && id === sessionId);
    return api.connectWorkbenchEvents({
      onConnected: refreshAll,
      onMessageNew: (row) => {
        if (!mine(row.session_id)) return;
        sources.tail.noteLiveMerge();
        setTail((current) => mergeRow(current, row));
        if (!tailLoadedRef.current) sources.tail.refresh();
      },
      onMessageUpdated: (row) => {
        if (!mine(row.session_id)) return;
        sources.tail.noteLiveMerge();
        setTail((current) => ({
          ...current,
          messages: current.messages.map((existing) => (existing.id === row.id ? row : existing)),
        }));
        if (!tailLoadedRef.current) sources.tail.refresh();
      },
      // A mark-read in any window (this one included) changes `read_at` on
      // rows the pet holds, and only a read brings that in.
      onInboxUnreadChanged: ({ session_id }) => {
        if (mine(session_id)) sources.tail.refresh();
      },
      onSessionStatus: ({ session_id, agent_status }) => {
        if (!mine(session_id)) return;
        if (!sessionRef.current) {
          sources.session.refresh();
          return;
        }
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
      // once, then resolve main again. Its owner-only grant must land before any
      // transcript/runtime reads under the new authorization.
      onAuthorizationChanged: () => {
        // Invalidate callbacks synchronously, before React commits the reset.
        authorizationNow.current += 1;
        setAuthorizationGeneration(authorizationNow.current);
        setSession(null);
        setCanChat(null);
        setTail(EMPTY_TAIL);
        setTurn(null);
        setInGrace(false);
        sources.tail.setKey(null);
        sources.turn.setKey(null);
        sources.session.setKey(authorizationNow.current);
        sources.session.refresh();
      },
      onQueueUpdated: ({ session_id }) => {
        if (mine(session_id)) sources.turn.refresh();
      },
      // Background activities are owned by S through definitions and callback
      // runs, which neither event names: re-read on every one, coalesced.
      onRunsUpdated: () => sources.turn.refresh(),
      onDefinitionsUpdated: () => sources.turn.refresh(),
    });
  }, [api, sessionId, resolved, authorizationGeneration, sources, refreshAll, sessionRef, tailLoadedRef]);

  // The window coming back is a gap too.
  useEffect(() => {
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
      if (!epoch.isCurrent()) return;
      setInGrace(false);
      sources.turn.refresh();
    }, SEND_GRACE_MS);
    return () => window.clearTimeout(timer);
  }, [sendCount, sources, epoch]);

  // Interval reconcile only while work is present and the window is visible.
  useEffect(() => {
    if (!sessionId || (!working && !hasBackground)) return undefined;
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') sources.turn.refresh();
    }, hasBackground ? ACTIVITY_RECONCILE_INTERVAL_MS : WORKING_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [sessionId, working, hasBackground, sources]);

  const noteSent = useCallback((row: WorkbenchMessage | null) => {
    if (!epoch.isCurrent()) return;
    setInGrace(true);
    setSendCount((count) => count + 1);
    if (row && row.session_id === sessionId && row.id) {
      sources.tail.noteLiveMerge();
      setTail((current) => mergeRow(current, row));
    }
    sources.tail.refresh();
    sources.turn.refresh();
  }, [sources, sessionId, epoch]);

  const refreshTail = useCallback(() => { if (epoch.isCurrent()) sources.tail.refresh(); }, [sources, epoch]);

  return {
    epoch,
    session,
    canChat,
    messages: tail.messages,
    hasOlder: tail.hasOlder || !tail.loaded,
    /** The bound session's tail has been read at least once. */
    tailLoaded: tail.loaded,
    turn,
    running: working,
    noteSent,
    refreshTail,
  };
}
