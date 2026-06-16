import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { AppWindow, ArrowLeft, Bell, Bot, ChevronDown, Clock, Info, Loader2, MessageSquare, Pencil, UploadCloud, X } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { useWorkbenchInbox } from '../../context/WorkbenchInboxContext';
import { useRegisterComposerTarget, type ComposerInsertTarget } from '../../context/ComposerBridgeContext';
import type { VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { apiFetch } from '../../lib/apiFetch';
import { normalizeChatMessageFontSize } from '../../lib/chatDisplay';
import { useIosKeyboardInset } from '../../lib/useIosKeyboardInset';
import { isProxyMediaUrl } from '../../lib/mediaProxy';
import { formatLocalDateTime } from '../../lib/relativeTime';
import { useFileDrop } from '../../lib/useFileDrop';
import { AgentRoutePicker } from './AgentRoutePicker';
import { InstallHint } from '../InstallHint';
import { Button } from '../ui/button';
import { ChatImage } from '../ui/chat-image';
import { FileCard } from '../ui/file-card';
import { ImageViewerProvider } from '../ui/image-viewer';
import { FileViewerProvider } from '../ui/file-viewer';
import { Input } from '../ui/input';
import { Markdown } from '../ui/markdown';
import { Composer, type ComposerAttachment, type ComposerHandle, type ComposerProps } from './Composer';
import type { MentionReference } from '../../lib/mentions';
import { QuickReplies } from './QuickReplies';

// While a turn is in flight, reconcile the working/Stop state against the
// controller on this cadence (the backend ``GET /turn-state`` is authoritative).
// This recovers a DROPPED ``turn.end`` without ever killing a live turn on a
// timer: there is no turn-duration timeout, so a long agent (which can run for
// hours) keeps Stop + the indicator for as long as ``/turn-state`` reports
// ``in_flight:true``; only an idle reading (past the post-send grace) clears it.
const WORKING_RECONCILE_INTERVAL_MS = 60 * 1000;

// Grace window after we optimistically set ``working`` from a local send before
// an idle ``/turn-state`` reading is trusted to CLEAR it. A just-sent turn isn't
// registered in the controller's in-flight map until POST→dispatch_async lands,
// so an idle snapshot taken inside that gap is a false negative — wait this long
// (comfortably above dispatch latency) before letting a reconnect/visibility
// idle check clear Stop. A genuinely stale turn (missed ``turn.end``) was set
// working far longer ago than this, so it still clears (Codex P2).
const WORKING_SETTLE_GRACE_MS = 4000;

// The transcript-visible message types — mirrors the server filter on
// ``GET /api/sessions/{id}/messages`` so the live ``message.new`` feed appends
// the same rows the initial load shows (assistant / tool_call are process log).
const isTranscriptMessage = (msg: WorkbenchMessage): boolean =>
  msg.type === 'user' ||
  msg.type === 'result' ||
  msg.type === 'error' ||
  msg.type === 'notify' ||
  (msg.metadata as { source?: string } | null)?.source === 'show_page';

// Durable transcript order: ``created_at`` is second-resolution, so the
// message id (a microsecond-clock prefix, see messages_service._new_message_id)
// is the tie-break — matching the server's ``(created_at, id)`` ordering.
const byCreatedThenId = (a: WorkbenchMessage, b: WorkbenchMessage): number => {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
  if (a.id === b.id) return 0;
  return a.id < b.id ? -1 : 1;
};

// Union two row sets, deduped by id and re-sorted into durable order. Used for
// the initial snapshot + live merge AND every live append, so a fast agent
// result that arrives over /api/events *before* its prompt row still lands in
// the correct position instead of ahead of the prompt (Codex P2). Also closes
// the load/subscribe race where a blind setMessages(snapshot) would clobber a
// message that arrived over the stream before the REST load returned.
const mergeById = (existing: WorkbenchMessage[], incoming: WorkbenchMessage[]): WorkbenchMessage[] => {
  const seen = new Set(existing.map((m) => m.id));
  const merged = [...existing, ...incoming.filter((m) => !seen.has(m.id))];
  merged.sort(byCreatedThenId);
  return merged;
};

// Mirrors design.pen kxEkn — the inline header replaces the old "Session
// settings" dialog. Title is click-to-edit; the cyan-bordered pill on the
// right opens a single popover that drives agent / model / effort all at
// once so the user doesn't have to navigate three different menus.
//
// Transcript model (session/page-scoped, NOT per-turn): on mount we load the
// persisted history once, then subscribe to this session's ``message.new`` for
// as long as the page is open — so EVERY message lands live, including agent
// replies the user didn't trigger (scheduled task / watch / proactive). Sending
// is a plain fire-and-forget POST; the reply arrives over the same stream.
export const ChatPage: React.FC = () => {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const api = useApi();
  const { unreadBySession, markRead: markInboxRead } = useWorkbenchInbox();
  // The mobile chat surface is a fixed full-screen flex column; this keeps the
  // composer glued to the iOS keyboard (settle-then-correct; see the hook).
  const chatSurfaceRef = useRef<HTMLDivElement>(null);
  useIosKeyboardInset(chatSurfaceRef);

  // Chat-page-wide drag-and-drop: dropping files anywhere over the chat surface
  // (not just the input row) stages them on the composer via its imperative
  // handle. Desktop-only in practice — touch fires no drag events — and disabled
  // until a session exists (the upload endpoint is session-scoped).
  const composerRef = useRef<ComposerHandle>(null);
  const { dragging: fileDragging, handlers: fileDropHandlers } = useFileDrop(
    (files) => composerRef.current?.addFiles(files),
    { disabled: !sessionId },
  );

  // Loaded session (null while bootstrapping — ChatPage renders a loader until
  // it's set). Lifted above the composer bridge + show-page logic that gate on it.
  const [session, setSession] = useState<WorkbenchSession | null>(null);

  // Show Page toggle: swap the chat surface (transcript + composer, NOT the
  // header bar) for this session's Show Page in an iframe, and back. Declared
  // before the composer bridge target, which depends on showPageMode.
  const [showPageMode, setShowPageMode] = useState(false);
  const [showPageBusy, setShowPageBusy] = useState(false);
  // Sessions whose first-open visualize prompt failed to send — retry it on the
  // next toggle (the page row already exists, so `existed` alone won't re-prompt).
  const showPagePromptRetryRef = useRef<Set<string>>(new Set());
  const [showPageUrl, setShowPageUrl] = useState<string | null>(null);
  useEffect(() => {
    // ChatPage is reused across :sessionId — clear all show-page state so the
    // next chat starts in chat view with a live (not stuck-busy) toggle.
    setShowPageMode(false);
    setShowPageUrl(null);
    setShowPageBusy(false);
  }, [sessionId]);

  // Publish this chat's composer to the ComposerBridge so the sidebar's
  // "reference this session" action can insert a #<session> mention into the
  // open chat's input.
  const insertSessionReference = useCallback(
    (refSessionId: string, title?: string | null) =>
      composerRef.current?.insertSessionReference(refSessionId, title),
    [],
  );
  // Null target hides that sidebar action unless the composer is actually
  // mounted + insertable: a chat is open (sessionId), its session has loaded
  // (before that ChatPage shows a loader — the composer isn't rendered yet), and
  // the Show Page iframe hasn't replaced the composer. Otherwise an insert would
  // silently no-op against a null composerRef.
  const composerTarget = useMemo<ComposerInsertTarget | null>(
    () => (sessionId && session != null && !showPageMode ? { sessionId, insertSessionReference } : null),
    [sessionId, session, showPageMode, insertSessionReference],
  );
  useRegisterComposerTarget(composerTarget);

  // Back returns to the page the user came from, not a hardcoded inbox.
  // location.key === 'default' means /chat was the first history entry (deep
  // link / refresh) with nothing to pop back to — fall back to the inbox then.
  const goBack = useCallback(() => {
    if (location.key !== 'default') navigate(-1);
    else navigate('/inbox');
  }, [location.key, navigate]);

  const [agents, setAgents] = useState<VibeAgentBrief[]>([]);
  const [defaultAgentName, setDefaultAgentName] = useState<string | null>(null);
  const [messages, setMessages] = useState<WorkbenchMessage[]>([]);
  const [olderCursor, setOlderCursor] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const loadingOlderRef = useRef(false);
  const oldestLoadedIdRef = useRef<string | null>(null);
  const newestLoadedIdRef = useRef<string | null>(null);
  const [messageFontSize, setMessageFontSize] = useState(() => normalizeChatMessageFontSize(undefined));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // ``working`` = a turn is in flight for this session (from our send, or any
  // other origin we observe). Drives the thinking bubble + the Send→Stop swap.
  const [working, setWorking] = useState(false);
  // Bumped on resume (tab visible again / network back) to force the transcript
  // subscription effect to reopen a possibly-dead SSE stream — see the
  // visibility effect below.
  const [connectionEpoch, setConnectionEpoch] = useState(0);
  // Lifecycle guards for ``syncTurnState``'s clear-on-idle (Codex P2):
  //  - ``turnEpochRef`` bumps every time a turn STARTS (local send / send-now /
  //    observed ``turn.start``). syncTurnState captures it before its request and
  //    refuses to clear if it changed meanwhile — so an idle snapshot can't stomp
  //    a turn that started WHILE the request was in flight.
  //  - ``workingSetAtRef`` records when we last set working true, so syncTurnState
  //    can ignore an idle reading that lands inside the post-send registration gap.
  const turnEpochRef = useRef(0);
  const workingSetAtRef = useRef(0);
  // A single pending "re-check after the post-send grace expires" timer + a ref
  // to the latest syncTurnState, so an idle reading that arrives INSIDE the grace
  // (which we can't trust to clear yet) still gets re-evaluated once the grace
  // passes — otherwise a quick turn whose turn.end was missed leaves Stop stuck
  // until the next reconcile poll (Codex P2).
  const graceResyncRef = useRef<number | null>(null);
  const syncTurnStateRef = useRef<(() => void) | null>(null);
  // Mark a turn as live: bump the epoch + stamp the time, then show Stop. Used by
  // every "a turn is starting now" path so clear-on-idle stays race-safe.
  const markWorking = useCallback(() => {
    turnEpochRef.current += 1;
    workingSetAtRef.current = Date.now();
    setWorking(true);
  }, []);
  // Send-while-busy queue (messages sent while a turn runs, shown above the
  // composer) + the loaded draft to seed the composer with.
  const [queue, setQueue] = useState<WorkbenchMessage[]>([]);
  const [initialDraft, setInitialDraft] = useState<string | null>(null);
  const draftTimerRef = useRef<number | null>(null);
  // The debounced draft save still owed to the server, tagged with the session
  // it belongs to — so a fast session switch flushes it instead of dropping it.
  const draftPendingRef = useRef<{ sessionId: string; text: string } | null>(null);
  // Tracks which session's handed-off initial message we've already replayed
  // (see the initial-message effect below). Keyed by session id, not a global
  // boolean, so a second create-via-chat flow that reuses this ChatPage
  // instance (React Router swaps only the :sessionId) still fires.
  const initialHandledSessionRef = useRef<string | null>(null);
  // The session the component is currently on. Async loads capture their
  // request's sessionId and compare against this before committing state, so a
  // load that resolves after the user switched chats can't leak the previous
  // session's rows into the current one (Codex P2).
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;

  const appendMessage = useCallback((msg: WorkbenchMessage) => {
    // Dedupe by id (a sent user row is appended optimistically AND echoed over
    // the stream) and keep durable (created_at, id) order so an out-of-order
    // live event can't render a reply ahead of its prompt.
    setMessages((prev) => (prev.some((m) => m.id === msg.id) ? prev : mergeById(prev, [msg])));
  }, []);

  // The header's backend lock keys on ``native_session_id``, which the FIRST
  // turn binds server-side with no dedicated event — so an open page wouldn't
  // learn it until reload and the picker would keep offering switches the
  // server now rejects (409). Until the native is known, refresh the row at
  // the recovery points (turn end / reconnect / tab visible). No-op for the
  // common already-bound session.
  const hasNativeRef = useRef(false);
  useEffect(() => {
    hasNativeRef.current = Boolean(session?.native_session_id);
  }, [session]);
  const refreshSessionRowUntilNativeBound = useCallback(async () => {
    const id = sessionIdRef.current;
    if (!id || hasNativeRef.current) return;
    try {
      // cache:false — an earlier refresh (page open / reconnect) may have
      // cached the still-native-less row; a quick turn ending inside the read
      // cache's TTL would reuse it and leave the picker unlocked.
      const row = await api.getSession(id, { cache: false });
      setSession((prev) => (prev && prev.id === row.id && row.id === sessionIdRef.current ? row : prev));
    } catch {
      // Best-effort: the next recovery point retries.
    }
  }, [api]);

  useEffect(() => {
    oldestLoadedIdRef.current = messages[0]?.id ?? null;
    newestLoadedIdRef.current = messages[messages.length - 1]?.id ?? null;
  }, [messages]);

  // Reconcile against durable storage after a window where ``message.new`` could
  // have been missed — the SSE broker is an in-memory fan-out with no replay, so
  // a reconnect or a backgrounded mobile tab can drop events while the reply is
  // safely in SQLite. Re-fetches the RECENT WINDOW (not just rows after a cursor)
  // and merges (deduped), so a missed EARLIER row — a flushed queued prompt, or a
  // prompt sent from another tab — is recovered even if a later row already
  // arrived; a cursor-after query would skip past the gap forever (Codex P2).
  // Does NOT touch ``working``: ``turn.end`` is the authoritative end signal, and
  // clearing on a fetched (possibly older) result could hide Stop on a newer
  // queued turn that is still in flight (Codex P2). Cheap + idempotent.
  const reconcile = useCallback(async () => {
    if (!sessionId) return;
    try {
      // tail: the RECENT window (not the oldest page), so a missed latest row in
      // a long chat is actually recovered (Codex P2).
      const res = await api.listSessionMessages(sessionId, { limit: 50, tail: true, cache: false });
      if (sessionId !== sessionIdRef.current) return; // switched chats mid-fetch
      const fresh = res.messages.filter(isTranscriptMessage);
      if (fresh.length) {
        const tailOldestId = fresh[0].id;
        const previousOldestId = oldestLoadedIdRef.current;
        const previousNewestId = newestLoadedIdRef.current;
        setMessages((prev) => mergeById(prev, fresh));
        if (
          previousOldestId &&
          previousNewestId &&
          tailOldestId > previousNewestId
        ) {
          setOlderCursor(res.next_before_id ?? null);
        }
      }
    } catch {
      /* keep the current transcript; the next reconnect retries */
    }
  }, [api, sessionId]);

  // The send-while-busy queue (pending messages shown above the composer).
  // Re-fetched on mount + on every ``queue.updated`` (enqueue / flush / remove).
  const refreshQueue = useCallback(async () => {
    if (!sessionId) return;
    try {
      const res = await api.listSessionQueue(sessionId, { cache: false });
      if (sessionId !== sessionIdRef.current) return; // switched chats mid-fetch
      setQueue(res.queued ?? []);
    } catch {
      /* leave the last-known queue; the next queue.updated refetches */
    }
  }, [api, sessionId]);

  const loadOlderMessages = useCallback(async () => {
    if (!sessionId || !olderCursor || loadingOlderRef.current) return;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    try {
      const res = await api.listSessionMessages(sessionId, { limit: 50, beforeId: olderCursor });
      if (sessionId !== sessionIdRef.current) return; // switched chats mid-fetch
      const older = res.messages.filter(isTranscriptMessage);
      if (older.length) {
        setMessages((prev) => mergeById(prev, older));
      }
      setOlderCursor(res.next_before_id ?? null);
    } catch {
      /* keep the current transcript; another scroll can retry */
    } finally {
      if (sessionId === sessionIdRef.current) {
        loadingOlderRef.current = false;
        setLoadingOlder(false);
      }
    }
  }, [api, olderCursor, sessionId]);

  // Persist the composer's unsent text server-side (debounced) so it survives a
  // reload / device switch. The send path clears it server-side; this only
  // saves while typing.
  const onDraftChange = useCallback(
    (text: string) => {
      if (!sessionId) return;
      // Tag the pending save with THIS session so the timer (and the
      // session-change flush) save to the right session even if the user has
      // since navigated away.
      draftPendingRef.current = { sessionId, text };
      if (draftTimerRef.current) window.clearTimeout(draftTimerRef.current);
      draftTimerRef.current = window.setTimeout(() => {
        const pending = draftPendingRef.current;
        draftPendingRef.current = null;
        draftTimerRef.current = null;
        if (pending) void api.setSessionDraft(pending.sessionId, pending.text);
      }, 600);
    },
    [api, sessionId],
  );

  // Flush a still-pending draft for the session we're leaving, so switching
  // chats within the debounce window doesn't drop it (Codex P2). Runs on
  // sessionId change + unmount.
  useEffect(() => {
    return () => {
      if (draftTimerRef.current) {
        window.clearTimeout(draftTimerRef.current);
        draftTimerRef.current = null;
      }
      const pending = draftPendingRef.current;
      draftPendingRef.current = null;
      if (pending) void api.setSessionDraft(pending.sessionId, pending.text);
    };
  }, [sessionId, api]);

  // The fire-and-forget turn survives browser disconnects, so a freshly loaded /
  // reconnected page asks the controller whether a turn is still in flight and
  // restores the working/Stop state to match (Codex P2). Authoritative in BOTH
  // directions: sets Stop when a turn is live, and clears a stale Stop (a
  // ``turn.end`` we missed while the socket was down) when the controller reports
  // idle — guarded so it can't drop a turn that's genuinely starting.
  const syncTurnState = useCallback(async () => {
    if (!sessionId) return;
    const epochAtRequest = turnEpochRef.current;
    try {
      const res = await api.getTurnState(sessionId);
      if (sessionId !== sessionIdRef.current) return;
      if (res.in_flight === null) return;
      if (res.in_flight) {
        // markWorking (not setWorking): bump the epoch + timestamp so an OLDER
        // overlapping sync whose idle response lands AFTER this one can't clear
        // the Stop we just confirmed live — its captured epoch is now stale (P2).
        markWorking();
        return;
      }
      // Idle snapshot — clear the stale indicator, but only when it's safe:
      //  (1) no turn STARTED while this request was in flight (epoch unchanged) —
      //      otherwise we'd stomp a turn.start that raced our idle reading;
      //  (2) we're past the post-send registration grace — a turn we just sent may
      //      not be in the controller's in-flight map yet, making this idle a
      //      false negative.
      if (turnEpochRef.current !== epochAtRequest) return;
      const sinceSet = Date.now() - workingSetAtRef.current;
      if (sinceSet > WORKING_SETTLE_GRACE_MS) {
        setWorking(false);
      } else if (graceResyncRef.current === null) {
        // Idle INSIDE the grace: either the registration gap (don't clear) or a
        // quick turn that already finished and whose turn.end we missed (a
        // backgrounded tab). Re-check once the grace expires so the latter clears
        // instead of waiting out the next reconcile poll. One pending retry at a time.
        graceResyncRef.current = window.setTimeout(() => {
          graceResyncRef.current = null;
          syncTurnStateRef.current?.();
        }, WORKING_SETTLE_GRACE_MS - sinceSet + 50);
      }
    } catch {
      /* controller unreachable — leave the indicator as-is */
    }
  }, [api, sessionId, markWorking]);

  // Keep a ref to the latest syncTurnState so the grace-resync timer can call the
  // current closure without baking it into a dependency cycle.
  useEffect(() => {
    syncTurnStateRef.current = syncTurnState;
  }, [syncTurnState]);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    setLoading(true);
    setError(null);
    try {
      // Initial chat open needs the same recent tail window, queue, draft,
      // route/config state, and current turn state. Fetch them as one bootstrap
      // payload so remote links don't pay a tunnel round-trip per widget.
      const bootstrap = await api.getSessionBootstrap(sessionId);
      // Dropped if the user switched chats while this load was in flight.
      if (sessionId !== sessionIdRef.current) return;
      setSession(bootstrap.session);
      setAgents(bootstrap.agents);
      setDefaultAgentName(bootstrap.default_agent_name);
      setMessageFontSize(normalizeChatMessageFontSize(bootstrap.config?.ui?.chat_message_font_size));
      // Merge (not replace) so a row that arrived over the stream during the
      // load isn't clobbered; the session-change reset keeps prior sessions out.
      setMessages((prev) => mergeById(bootstrap.messages, prev));
      setOlderCursor(bootstrap.next_before_id ?? null);
      setQueue(bootstrap.queued ?? []);
      setInitialDraft(bootstrap.draft?.text ?? '');
      // Restore Stop for a turn that is still running (e.g. opened in another tab
      // or reloaded mid-turn). markWorking on the live branch so a racing
      // syncTurnState idle response can't clear it; an idle load is authoritative
      // for the fresh page, so clear directly (Codex P2).
      if (bootstrap.turn_state.in_flight) markWorking();
      else if (bootstrap.turn_state.in_flight === false) setWorking(false);
    } catch (err: any) {
      // Only surface the error if we're still on the session that failed — a
      // stale failure must not stamp an error onto the chat the user moved to.
      if (sessionId === sessionIdRef.current) setError(err?.message ?? String(err));
    } finally {
      // Same guard: a stale load finishing must not flip the new session out of
      // its own loading state into a premature not-found / error view (Codex P2).
      if (sessionId === sessionIdRef.current) setLoading(false);
    }
  }, [api, sessionId, markWorking]);

  // Clear per-session state the instant the session changes (React Router swaps
  // only :sessionId, reusing this instance), before the new session's
  // load/subscribe — so the previous conversation / queue / draft never leak in
  // and the merge in ``refresh`` only ever unions same-session rows.
  useEffect(() => {
    // Clear ``session`` too (not just messages/queue/draft): otherwise the header
    // keeps rendering the previous chat's title + agent picker until the new load
    // finishes, and a rename / agent change would patch() the STALE session.id
    // while the URL is already on the new chat (Codex P2). Nulling it shows the
    // loading state until refresh() resolves the new session.
    setSession(null);
    setMessages([]);
    setOlderCursor(null);
    oldestLoadedIdRef.current = null;
    newestLoadedIdRef.current = null;
    loadingOlderRef.current = false;
    setLoadingOlder(false);
    setWorking(false);
    setQueue([]);
    setInitialDraft(null);
    // Drop any pending grace-resync so it can't fire against the new session.
    if (graceResyncRef.current !== null) {
      window.clearTimeout(graceResyncRef.current);
      graceResyncRef.current = null;
    }
  }, [sessionId]);

  // Persistent per-session subscription: append every transcript-visible
  // ``message.new`` for THIS session for as long as the page is open. An agent
  // ``result`` ends the working state (the turn produced its reply). Harness
  // turns (scheduled / watch) flow through here too — their prompt + reply both
  // appear without the user having sent anything.
  useEffect(() => {
    if (!sessionId) return;
    const disconnect = api.connectWorkbenchEvents({
      // NB: match against sessionIdRef.current (the CURRENT route), NOT the
      // captured ``sessionId`` — there is a window after a chat switch before
      // React runs this subscription's cleanup, during which an event for the
      // PREVIOUS chat would otherwise pass the stale check and append into the
      // new chat (Codex P2).
      onMessageNew: (msg) => {
        if (msg.session_id !== sessionIdRef.current) return;
        if (!isTranscriptMessage(msg)) return;
        appendMessage(msg);
        // Don't clear ``working`` from a result row here: with the queue, a
        // result can belong to an EARLIER turn while a newer queued turn is
        // already running, so clearing on it would hide Stop on the live turn
        // (Codex P2). ``turn.end`` is the authoritative end signal; a dropped
        // turn.end is recovered by syncTurnState (reconnect / visibility / the
        // while-working reconcile poll).
      },
      onTurnStart: (data) => {
        // markWorking (not setWorking): bump the epoch so a syncTurnState idle
        // reading already in flight can't clear this freshly-started turn.
        if (data.session_id === sessionIdRef.current) markWorking();
      },
      onTurnEnd: (data) => {
        // The controller confirms the turn settled (terminal result, agent error,
        // or user cancel) — the authoritative end of the working state. There is
        // no turn-duration timeout, so this only fires on a REAL terminal signal.
        if (data.session_id === sessionIdRef.current) {
          setWorking(false);
          // The first turn binds the native; pick it up so the header's backend
          // lock engages without a reload. A failed first turn leaves no native
          // (the refresh confirms that), keeping the backend switchable so the
          // user can recover by re-routing.
          void refreshSessionRowUntilNativeBound();
        }
      },
      onQueueUpdated: (data) => {
        // The send-while-busy queue changed (enqueue / flush / per-item delete).
        if (data.session_id === sessionIdRef.current) void refreshQueue();
      },
      onSessionActivity: (data) => {
        if (data.session_id === sessionIdRef.current && data.event === 'archived') {
          // The session you're viewing was archived (here or in another tab) —
          // archive is terminal, so leave the chat.
          goBack();
          return;
        }
        // A rename (from the sidebar or elsewhere) broadcasts the new title;
        // keep this chat's header in sync without a reload. Match the CURRENT
        // route via sessionIdRef like the handlers above.
        if (data.session_id !== sessionIdRef.current || data.event !== 'updated') return;
        if (!Object.prototype.hasOwnProperty.call(data, 'title')) return;
        const nextTitle = data.title ?? null;
        setSession((prev) => {
          if (!prev || prev.id !== data.session_id || prev.title === nextTitle) return prev;
          return { ...prev, title: nextTitle };
        });
      },
      onConnected: () => {
        // Every (re)connect recovers any state missed while the socket was down:
        // dropped message rows, the queue, whether a turn is still running, and
        // a native bind whose turn.end we missed.
        void reconcile();
        void refreshQueue();
        void syncTurnState();
        void refreshSessionRowUntilNativeBound();
      },
      onError: () => {
        // Browser EventSource auto-reconnects; keep the page usable.
      },
    }, { reconnect: connectionEpoch > 0 });
    return disconnect;
  }, [api, sessionId, appendMessage, reconcile, refreshQueue, syncTurnState, refreshSessionRowUntilNativeBound, markWorking, connectionEpoch, goBack]);

  // Mobile tabs (the common case for IM users) get backgrounded mid-turn; the
  // SSE feed can be suspended without a clean reconnect, dropping the reply.
  // Reconcile when the page becomes visible again so the answer + working state
  // catch up to durable storage.
  useEffect(() => {
    if (!sessionId) return;
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return;
      // A suspended tab can drop the reply AND the turn.end, so recover all
      // three: missed rows, the queue, and the working/Stop state (Codex P2).
      void reconcile();
      void refreshQueue();
      void syncTurnState();
      // The immediate reconcile above only catches up to NOW; if the socket
      // itself is dead (iOS can leave EventSource in a zombie OPEN state that
      // never auto-reconnects), it would be the last update until the next
      // resume. Reopen the stream so live events keep flowing while the page
      // stays foregrounded — the reopen's onConnected re-runs reconcile
      // (idempotent).
      setConnectionEpoch((e) => e + 1);
    };
    document.addEventListener('visibilitychange', onVisible);
    // Network flaps (mobile handoff, sleep/wake) can kill the stream without a
    // visibility change; reopen it on ``online`` too (only while foregrounded —
    // a background resume is handled by visibilitychange).
    const onOnline = () => {
      if (document.visibilityState === 'visible') setConnectionEpoch((e) => e + 1);
    };
    window.addEventListener('online', onOnline);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('online', onOnline);
    };
  }, [sessionId, reconcile, refreshQueue, syncTurnState]);

  // Reconcile (don't kill) while a turn is in flight: there is no turn-duration
  // timeout, so a long agent can run for hours and must keep Stop + the indicator
  // the whole time. Instead of a force-clear timer, poll the controller's
  // authoritative ``GET /turn-state`` on an interval while ``working`` is true AND
  // the page is visible. ``syncTurnState``'s grace-guarded logic clears ``working``
  // only when the backend reports ``in_flight:false`` — so a dropped ``turn.end``
  // is recovered, while a still-running turn keeps Stop. Cleared when ``working``
  // flips false / on unmount; skipped while hidden (visibilitychange already
  // reconciles on resume).
  useEffect(() => {
    if (!working) return;
    const interval = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      void syncTurnState();
    }, WORKING_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [working, syncTurnState]);

  const sendMessage = useCallback(
    async (
      text: string,
      attachments?: ComposerAttachment[],
      metadata?: Record<string, unknown>,
      references?: MentionReference[],
    ) => {
      // NB: no ``working`` guard — sending WHILE a turn runs is the queue
      // feature; the backend enqueues it (202) instead of refusing.
      const ready = (attachments ?? []).filter((a) => a.status === 'ready');
      if (!sessionId || (!text.trim() && ready.length === 0)) return;
      markWorking();
      setError(null);
      try {
        // Plain (non-streaming) POST: the turn runs fire-and-forget on the
        // controller and its reply arrives over the persistent ``message.new``
        // stream — we don't hold the response open. ``apiFetch`` attaches the
        // CSRF token that ``protect_mutating_ui_requests`` requires under
        // remote-access mode (raw ``fetch`` would 403).
        const refs = references ?? [];
        const content =
          ready.length > 0 || refs.length > 0
            ? {
                text,
                ...(ready.length > 0
                  ? {
                      attachments: ready.map((a) => ({
                        token: a.token,
                        name: a.name,
                        mime: a.mime,
                        size: a.size,
                        kind: a.kind,
                        url: a.url,
                        // Persist image pixel size when known so the box is reserved on
                        // reload (undefined keys drop out of the JSON).
                        width: a.width,
                        height: a.height,
                      })),
                    }
                  : {}),
                // @-agent / #-session mention sidecar (see lib/mentions): the text
                // keeps the `@<name>` / `#<id>` markers; this carries resolved ids +
                // session titles for chip rendering and the backend reference block.
                ...(refs.length > 0 ? { references: refs } : {}),
              }
            : undefined;
        const requestBody = {
          text,
          ...(content ? { content } : {}),
          // Quick-reply click: tag the user row with the agent message it answers
          // so the locked/highlighted state can be derived on reload.
          ...(metadata ? { metadata } : {}),
        };
        const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody),
        });
        const body = await response.json().catch(() => null);
        // If the user switched chats while this POST was in flight, the response
        // belongs to the previous session — don't append it / mutate working /
        // error on the chat they moved to (Codex P2). The turn still ran for the
        // original session; its rows live there.
        if (sessionId !== sessionIdRef.current) return;
        if (!response.ok) {
          setWorking(false);
          throw new Error(body?.detail ? String(body.detail) : `HTTP ${response.status}`);
        }
        if (body?.already_answered) {
          // A duplicate quick-reply the backend already had (stale tab / missed
          // event): no turn started HERE. Reconcile authoritatively rather than
          // force-clearing — a genuinely-running turn (e.g. clicking an old group
          // while a turn runs) must keep its Stop/thinking state. Return false so
          // the quick-reply group drops its optimistic lock instead of staying
          // stuck highlighting the rejected choice.
          syncTurnStateRef.current?.();
          return false;
        }
        if (body?.queued) {
          // Sent while a turn was running → enqueued (shows above the composer
          // via queue.updated). A turn IS in flight, so keep working/Stop; don't
          // add a transcript row. Refresh immediately in case the event races.
          void refreshQueue();
          return;
        }
        // A turn started — optimistically show the user row (echo dedupes by id).
        if (body && body.id) appendMessage(body as WorkbenchMessage);
      } catch (err: any) {
        if (sessionId === sessionIdRef.current) {
          setWorking(false);
          setError(err?.message ?? String(err));
          // Signal the composer the send didn't start so it restores the text +
          // uploaded chips — the user can retry without re-uploading (Codex r5).
          return false;
        }
      }
    },
    [sessionId, appendMessage, refreshQueue, markWorking],
  );

  // @ mention source: all enabled Agents, filtered client-side (the set is small
  // and already loaded for this session via bootstrap).
  const searchAgents = useCallback(
    async (query: string) => {
      const q = query.trim().toLowerCase();
      return agents
        .filter((a) => a.enabled)
        // Names with the marker terminator (`>`) or a newline can't round-trip
        // through @<name>, so they aren't mentionable.
        .filter((a) => !/[>\n]/.test(a.name))
        .filter((a) => !q || a.name.toLowerCase().includes(q))
        .map((a) => ({ name: a.name, agent_id: a.id, backend: a.backend, description: a.description }));
    },
    [agents],
  );

  // # reference source: recent active sessions machine-wide (excluding the current
  // one); ≥2 chars switches to a global title search via the server-side ``q``.
  const searchSessions = useCallback(
    async (query: string) => {
      const q = query.trim();
      const broad = q.length >= 2;
      const res = await api.listSessions(
        broad
          ? { q, status: 'active', limit: 24, cache: false }
          : { status: 'active', limit: 12, cache: true },
      );
      return res.sessions
        .filter((s) => s.id !== sessionId)
        .slice(0, broad ? 20 : 8)
        .map((s) => ({ session_id: s.id, title: s.title }));
    },
    [api, sessionId],
  );

  // Toggle the chat surface ↔ the session's Show Page (iframe). The first open
  // ensures the page exists; if it was just created, ask the agent to build the
  // visualization. Errors surface via the apiFetch toast layer.
  const toggleShowPage = useCallback(async () => {
    const sid = sessionId;
    if (!sid) return;
    if (showPageMode) {
      setShowPageMode(false);
      return;
    }
    setShowPageBusy(true);
    try {
      const res = await api.ensureShowPage(sid);
      // Bail if the user switched chats while ensure was in flight — otherwise a
      // stale resolve would flip the NEW chat into iframe mode + send its prompt.
      if (sessionIdRef.current !== sid) return;
      if (res?.ok) {
        // Public pages are served under /p/<share_id>/; private under /show/<id>/.
        setShowPageUrl(
          res.visibility === 'public' && res.share_id
            ? `/p/${encodeURIComponent(res.share_id)}/`
            : `/show/${encodeURIComponent(sid)}/`,
        );
        setShowPageMode(true);
        // First open (or a prior prompt that failed to send) asks the agent to
        // build the visualization. sendMessage returns false on a failed send;
        // track it so the NEXT toggle retries — the page row exists after this,
        // so `existed` alone would never re-prompt a created-but-unprompted page.
        if (res.existed === false || showPagePromptRetryRef.current.has(sid)) {
          void sendMessage(t('chat.showPage.prompt')).then((sent) => {
            if (sent === false) showPagePromptRetryRef.current.add(sid);
            else showPagePromptRetryRef.current.delete(sid);
          });
        }
      }
    } catch {
      // apiFetch already surfaced a toast; stay in chat view.
    } finally {
      // Always clear — the in-flight request is done regardless of which chat is
      // now mounted (ChatPage is reused across sessions; a guarded clear would
      // strand the shared busy flag on a session the user switched to).
      setShowPageBusy(false);
    }
  }, [sessionId, showPageMode, api, sendMessage, t]);

  // A quick-reply click sends the chosen label as a normal user turn, tagged with
  // the agent message it answers so the group can lock + highlight the choice on
  // reload (the answered state is derived from this metadata).
  const handleQuickReply = useCallback(
    // Send the chosen label as a normal user turn, tagged with the agent message
    // it answers. The backend records the choice on THAT agent message (the
    // message text is the label), so the lock derives from one authoritative
    // field. Returns sendMessage's result so the group can unlock on a failed send.
    (messageId: string, choice: string) => sendMessage(choice, undefined, { quick_reply_for: messageId }),
    [sendMessage],
  );

  const stopMessage = useCallback(async () => {
    if (!sessionId || !working) return;
    try {
      const res = await api.cancelSession(sessionId);
      // Drop a stale response after a chat switch — it must not clear B's
      // working or stamp A's error on B (Codex P2).
      if (sessionId !== sessionIdRef.current) return;
      // On success the backend is interrupted and the authoritative ``turn.end``
      // clears the working state, so we don't clear it here.
      if (res && res.status === 'stale_released') {
        setWorking(false);
        void syncTurnState();
      } else if (res && res.ok === false) {
        if (res.code === 'not_in_flight') {
          // The controller has no running turn — our working state was stale
          // (a missed turn.end). Clear it instead of leaving Stop stuck (Codex P2).
          setWorking(false);
          void syncTurnState();
        } else {
          // The stop didn't reach the backend (e.g. 503); the turn may still be
          // live, so keep Stop available + surface the failure.
          setError(res.detail ? String(res.detail) : t('chat.stopFailed'));
        }
      }
    } catch (err: any) {
      // The cancel request itself threw (network) — surface it; keep Stop.
      if (sessionId === sessionIdRef.current) setError(err?.message ?? String(err));
    }
  }, [api, sessionId, working, t, syncTurnState]);

  const removeQueued = useCallback(
    async (messageId: string) => {
      if (!sessionId) return;
      setQueue((prev) => prev.filter((m) => m.id !== messageId)); // optimistic
      try {
        await api.removeQueuedMessage(sessionId, messageId);
      } catch {
        void refreshQueue(); // restore on failure
      }
    },
    [api, sessionId, refreshQueue],
  );

  const sendQueueNow = useCallback(async () => {
    // "立即发送": interrupt the running turn + flush the queue now. The queue
    // flushes as one merged turn, so this runs the whole queue.
    if (!sessionId || queue.length === 0) return;
    // A turn is about to run (the flushed queue) — reflect it immediately so
    // Stop stays available even if the controller's turn.start is missed/delayed
    // (especially for the idle-flush case that starts a fresh turn) (Codex P2).
    markWorking();
    try {
      const res = await api.sendQueuedNow(sessionId, queue[0].id);
      // Drop the response if the user switched chats mid-request (Codex P2).
      if (sessionId !== sessionIdRef.current) return;
      if (res && res.ok === false) {
        // stop_failed: the controller left the ORIGINAL turn running and the
        // queue intact — keep Stop visible so the user can still interrupt it
        // (Codex P2). Other failures mean no turn is running → clear working.
        if (res.code !== 'stop_failed') setWorking(false);
        setError(res.detail ? String(res.detail) : t('chat.stopFailed'));
      } else if (res && (res as { status?: string }).status === 'empty') {
        // Nothing was actually flushed (a stale queue item already gone) — no
        // turn is starting, so drop the optimistic working state + resync.
        setWorking(false);
        void refreshQueue();
      }
    } catch (err: any) {
      // Same session guard as the success path: a rejection after a chat switch
      // must not clear the new chat's working / stamp this error on it (Codex P2).
      if (sessionId === sessionIdRef.current) {
        setWorking(false);
        setError(err?.message ?? String(err));
      }
    }
  }, [api, sessionId, queue, t, refreshQueue, markWorking]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // The user is actively viewing this session, so an agent reply here is seen,
  // not "new". Clear unread whenever it appears — on open, or when a realtime
  // inbox.session.updated lands after a reply — so the Inbox/sidebar never badge
  // the chat you're looking at. Reactive to the unread map, so it's race-free
  // against the cross-process event ordering.
  useEffect(() => {
    if (sessionId && (unreadBySession[sessionId] ?? 0) > 0) {
      void markInboxRead(sessionId);
    }
  }, [sessionId, unreadBySession, markInboxRead]);

  // The Workbench canvas creates the session and hands its first message over
  // as router state. Replay it once through the compose path so the agent turn
  // starts. Clear the state afterwards so a manual page refresh (which preserves
  // history state) doesn't resend it.
  useEffect(() => {
    const initialMessage = (location.state as { initialMessage?: string } | null)?.initialMessage;
    if (!initialMessage || !sessionId) return;
    if (initialHandledSessionRef.current === sessionId) return;
    if (loading || !session) return;
    initialHandledSessionRef.current = sessionId;
    navigate(location.pathname, { replace: true, state: null });
    void sendMessage(initialMessage);
  }, [location.state, location.pathname, loading, session, sessionId, navigate, sendMessage]);

  const patch = useCallback(
    async (changes: Partial<WorkbenchSession>) => {
      if (!session) return;
      const patchedId = session.id;
      try {
        const updated = await api.updateSession(session.id, changes as any);
        // Drop a stale response after a chat switch: if the user navigated to a
        // different chat (this ChatPage instance is reused) before the PATCH
        // resolved, installing A's session into B would show A's title/picker on
        // B and make later edits patch the wrong session.id (Codex P2). Mirrors
        // the sessionIdRef guards on send/cancel.
        if (patchedId !== sessionIdRef.current) return;
        setSession(updated);
      } catch (err: any) {
        if (patchedId === sessionIdRef.current) setError(err?.message ?? String(err));
      }
    },
    [api, session],
  );

  // Ordered media-proxy image URLs across the whole session — feeds the lightbox
  // so it pages left/right through every image, in render order (each message's
  // attachments first, then any inline images in its text).
  const sessionImages = useMemo(() => {
    const urls: string[] = [];
    const seen = new Set<string>();
    const push = (u: string) => {
      if (u && isProxyMediaUrl(u) && !seen.has(u)) {
        seen.add(u);
        urls.push(u);
      }
    };
    for (const m of messages) {
      const atts = (m.content as { attachments?: Array<Record<string, unknown>> })?.attachments;
      if (Array.isArray(atts)) {
        for (const a of atts) {
          if (a?.kind === 'image' || String(a?.mime || '').startsWith('image/')) push(String(a?.url || ''));
        }
      }
      if (m.text) {
        const re = /!\[[^\]]*\]\((\/api\/media\/[^)\s]+)\)/g;
        let match: RegExpExecArray | null;
        while ((match = re.exec(m.text)) !== null) push(match[1]);
      }
    }
    return urls;
  }, [messages]);

  if (!sessionId) {
    return <ChatMissing onBack={goBack} />;
  }

  // A direct session→session switch re-renders this SAME ChatPage instance with
  // the new :sessionId while every piece of state still belongs to the PREVIOUS
  // session — the reset effect only clears it after this render commits.
  // Rendering the chat body in those mismatch frames leaks the old session under
  // the new route: the composer remounts (key change) seeded with the OLD
  // session's draft and its seed-change would be persisted under the NEW
  // session id. Treat the mismatch as loading so nothing of the old session
  // ever mounts under the new route.
  if ((loading && !session) || (session && session.id !== sessionId)) {
    return (
      <div className="flex h-[60vh] flex-col items-center justify-center gap-2 text-muted">
        <Loader2 className="size-5 animate-spin" />
        <span className="text-[12px]">{t('common.loading')}</span>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 py-8">
        <button
          type="button"
          onClick={goBack}
          className="inline-flex items-center gap-1.5 text-[12px] text-cyan hover:underline"
        >
          <ArrowLeft className="size-3.5" />
          {t('chat.back')}
        </button>
        <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
          {error ?? t('chat.notFound')}
        </div>
      </div>
    );
  }

  return (
    // Fill the viewport so the transcript is the only scrolling region and
    // the compose bar genuinely anchors to the bottom. The outer AppShell
    // wraps every route in py-5/px-4 (mobile) and py-8/px-10 (desktop); we
    // cancel BOTH axes with negative margins so the header and compose bar
    // run edge-to-edge instead of leaving the page background showing
    // through on the left and right (regression feedback #4/#5).
    //
    // Height: on desktop the shell has no top bar (the mobile header is
    // ``md:hidden``) and ``-my-8`` already cancels the py-8, so the chat starts
    // at the viewport top — it must be a full ``100dvh`` tall. The previous
    // ``calc(100dvh-4rem)`` double-subtracted the (already-cancelled) padding
    // and left a 4rem dead gap below the compose bar. On mobile the sticky
    // ``h-16`` header occupies 4rem at the top, so subtract that instead.
    <ImageViewerProvider images={sessionImages}>
      <FileViewerProvider>
      {/* Mobile: a FIXED full-screen flex column (the AppShell brand header is
          hidden on chat) so the composer has NO scrollable ancestor — that is what
          let iOS fling it off the top. useIosKeyboardInset then sizes this surface
          to the visible area above the keyboard once it settles, so the composer
          stays glued to the keyboard. Desktop/iPad: revert to the in-flow layout
          sized to --app-vvh (the visual-viewport var handles the soft keyboard
          there). */}
      <div
        ref={chatSurfaceRef}
        className="fixed inset-0 z-40 flex flex-col bg-background pt-[env(safe-area-inset-top)] md:relative md:inset-auto md:z-auto md:-mx-10 md:-my-8 md:h-[var(--app-vvh)] md:bg-transparent md:pt-0"
        {...fileDropHandlers}
      >
        {/* Drag-and-drop overlay: shown while files hover anywhere over the chat
            surface. ``pointer-events-none`` lets the drag events bubble to this
            container, whose drop handler stages them on the composer. */}
        {fileDragging && (
          <div className="pointer-events-none absolute inset-0 z-10 m-2 flex items-center justify-center rounded-2xl border-2 border-dashed border-mint/60 bg-background/85 backdrop-blur-sm md:m-3">
            <div className="flex flex-col items-center gap-2 text-mint">
              <UploadCloud className="size-7" />
              <span className="text-[13px] font-medium">{t('chat.compose.dropOverlay')}</span>
            </div>
          </div>
        )}
        <ChatHeaderBar
          session={session}
          agents={agents}
          defaultAgentName={defaultAgentName}
          onPatch={patch}
          onBack={goBack}
          working={working}
          showPageMode={showPageMode}
          showPageBusy={showPageBusy}
          onToggleShowPage={toggleShowPage}
        />

      {showPageMode && showPageUrl && (
        // The session's Show Page (same-origin /show/<id>/ private or /p/<share>/
        // public; URL resolved from ensureShowPage) fills the chat area while the
        // header bar stays. The chat surface below is kept mounted but hidden.
        //
        // Sandbox is deliberately LIGHT: `allow-same-origin` is required (the page
        // authenticates with the workbench cookie + runs its own same-origin
        // fetches/WebSocket), and it intentionally also keeps the page able to
        // reach the parent — a Show Page interacting with the surrounding
        // workbench is a wanted (if not-yet-promoted) capability. Real isolation
        // would need a separate origin, which we won't do (Show Pages are part of
        // the product). The agent already has full machine access, so frontend
        // isolation isn't the security boundary anyway. We still drop the exotic
        // capabilities the page never needs (top navigation, pointer lock, etc.).
        <iframe
          title={t('chat.showPage.title')}
          src={showPageUrl}
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"
          className="min-h-0 w-full flex-1 border-0 bg-background"
        />
      )}

      {/* Chat surface stays MOUNTED while the Show Page is shown — just hidden —
          so unsent composer text + staged attachments survive the toggle instead
          of being discarded on unmount. */}
      <div className={clsx('flex min-h-0 flex-1 flex-col', showPageMode && 'hidden')}>
        {error && (
          <div className="mx-auto mt-3 w-full max-w-[1080px] rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
            {error}
          </div>
        )}

        <Transcript
          messages={messages}
          session={session}
          working={working}
          hasOlder={!!olderCursor}
          loadingOlder={loadingOlder}
          onLoadOlder={loadOlderMessages}
          messageFontSize={messageFontSize}
          onQuickReply={handleQuickReply}
        />
        <QueueStrip queue={queue} onRemove={removeQueued} onSendNow={sendQueueNow} />
        {/* key by session so the composer remounts per session — its draft-seeding
            + local value reset, instead of carrying across sessions (Codex P2). */}
        <Compose
          key={sessionId}
          composerRef={composerRef}
          onSend={(text, attachments, references) => sendMessage(text, attachments, undefined, references)}
          onStop={stopMessage}
          busy={working}
          sessionId={sessionId ?? ''}
          initialDraft={initialDraft}
          onDraftChange={onDraftChange}
          onSearchAgents={searchAgents}
          onSearchSessions={searchSessions}
        />
      </div>
      </div>
      </FileViewerProvider>
    </ImageViewerProvider>
  );
};

// Pending send-while-busy messages, shown between the transcript and the
// composer (Codex-GUI style). Each can be dropped; "立即发送" interrupts the
// running turn and flushes the whole queue now (the queue flushes merged).
// One queued message. Its text is a single truncated line by default; clicking
// it expands to the full wrapped text (and clicking again collapses it) so a
// long queued prompt can be read without sending it.
const QueueRow: React.FC<{ item: WorkbenchMessage; onRemove: (id: string) => void }> = ({ item, onRemove }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="flex items-start gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className={clsx(
          'min-w-0 flex-1 text-left text-[12px] text-foreground',
          expanded ? 'whitespace-pre-wrap break-words' : 'truncate',
        )}
      >
        {item.text}
      </button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={() => onRemove(item.id)}
        aria-label={t('chat.queue.remove')}
        className="size-6 shrink-0 text-muted hover:text-destructive"
      >
        <X className="size-3.5" />
      </Button>
    </div>
  );
};

const QueueStrip: React.FC<{
  queue: WorkbenchMessage[];
  onRemove: (id: string) => void;
  onSendNow: () => void;
}> = ({ queue, onRemove, onSendNow }) => {
  const { t } = useTranslation();
  if (queue.length === 0) return null;
  return (
    <div className="shrink-0 px-4 md:px-8">
      <div className="mx-auto w-full max-w-[1080px] rounded-xl border border-cyan/25 bg-cyan/[0.04] p-2">
        <div className="flex items-center justify-between px-1 pb-1.5">
          <span className="inline-flex items-center gap-1.5 font-mono text-[10px] font-bold uppercase tracking-[0.1em] text-cyan">
            <Clock className="size-3" />
            {t('chat.queue.title', { count: queue.length })}
          </span>
          <Button type="button" variant="ghost" size="sm" onClick={onSendNow} className="h-6 px-2 text-[11px] text-cyan">
            {t('chat.queue.sendNow')}
          </Button>
        </div>
        <div className="flex max-h-32 flex-col gap-1 overflow-y-auto">
          {queue.map((item) => (
            <QueueRow key={item.id} item={item} onRemove={onRemove} />
          ))}
        </div>
      </div>
    </div>
  );
};

interface ComposeProps {
  composerRef: React.Ref<ComposerHandle>;
  onSend: (text: string, attachments?: ComposerAttachment[], references?: MentionReference[]) => void;
  onStop: () => void;
  busy: boolean;
  sessionId: string;
  initialDraft: string | null;
  onDraftChange: (text: string) => void;
  onSearchAgents: ComposerProps['onSearchAgents'];
  onSearchSessions: ComposerProps['onSearchSessions'];
}

const Compose: React.FC<ComposeProps> = ({ composerRef, onSend, onStop, busy, sessionId, initialDraft, onDraftChange, onSearchAgents, onSearchSessions }) => (
  // shrink-0 pins the bar at the bottom of the fixed-height chat container; the
  // gradient fades the transcript out behind it (no opaque band / hard border)
  // so the input sits close to the bottom edge. The input row is the shared
  // <Composer>, also used by the Workbench home.
  <div
    className="shrink-0 px-4 pb-[calc(1rem+env(safe-area-inset-bottom))] pt-3 md:px-8 md:pb-4"
    style={{ background: 'linear-gradient(to top, var(--background) 65%, transparent)' }}
  >
    <Composer
      ref={composerRef}
      onSend={onSend}
      onStop={onStop}
      busy={busy}
      sessionId={sessionId}
      initialDraft={initialDraft}
      onDraftChange={onDraftChange}
      onSearchAgents={onSearchAgents}
      onSearchSessions={onSearchSessions}
      autoFocus
    />
  </div>
);

interface ChatHeaderBarProps {
  session: WorkbenchSession;
  agents: VibeAgentBrief[];
  defaultAgentName: string | null;
  onPatch: (changes: Partial<WorkbenchSession>) => Promise<void>;
  onBack: () => void;
  working: boolean;
  showPageMode: boolean;
  showPageBusy: boolean;
  onToggleShowPage: () => void;
}

const ChatHeaderBar: React.FC<ChatHeaderBarProps> = ({ session, agents, defaultAgentName, onPatch, onBack, working, showPageMode, showPageBusy, onToggleShowPage }) => {
  const { t } = useTranslation();
  const defaultAgent = defaultAgentName ? agents.find((agent) => agent.name === defaultAgentName) : null;
  // Backend locks once a NATIVE conversation exists — a native can only be
  // resumed by the backend that created it — or while a turn is RUNNING (the
  // in-flight turn binds its native on the current route any moment); mirrors
  // update_session's guard. Until then a session may carry a project-default
  // backend, but the user can still re-route it to any backend or clear back
  // to the default. A locked session with a KNOWN backend keeps the picker
  // open for same-backend agent/model changes; locked with a BLANK backend
  // (the global-default route mid-turn) has no valid choice at all — every
  // concrete pick would 409 — so the picker disables until the turn settles.
  // Idle blank-backend rows with a native (legacy, pre-backfill) stay enabled:
  // the server allows their one-time "initial pin".
  const concreteBackend = session.agent_backend?.trim() || null;
  const backendLocked = Boolean(session.native_session_id) || working;
  const pinnedBackend = backendLocked ? concreteBackend : null;
  const canClearToDefault = !backendLocked;
  const pickerDisabled = working && !concreteBackend;
  const defaultRoute = defaultAgent
    ? {
        agent_name: defaultAgent.name,
        agent_id: defaultAgent.id,
        agent_backend: defaultAgent.backend,
        agent_variant: defaultAgent.backend,
        model: defaultAgent.model,
        reasoning_effort: defaultAgent.reasoning_effort,
      }
    : undefined;
  const inheritsDefault = !session.agent_name && !session.agent_backend;
  return (
    // A single compact row (design.pen IDQ5n): back button + click-to-edit
    // title on the left, the agent/model/effort picker on the right. The bar
    // runs edge-to-edge (the page root cancels the shell padding) with a
    // hairline bottom border separating it from the scrolling transcript.
    // No project-id pill and no override banner — both were noise the user
    // flagged (regression feedback #1/#3).
    <div className="shrink-0 border-b border-border bg-surface/70 px-4 py-2.5 backdrop-blur md:px-8">
      <div className="mx-auto flex w-full max-w-[1080px] items-center gap-3">
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={onBack}
          aria-label={t('chat.back')}
          className="size-7 shrink-0"
        >
          <ArrowLeft className="size-3.5" />
        </Button>
        <TitleField key={session.id} title={session.title} onCommit={(title) => onPatch({ title })} />
        <AgentRoutePicker
          value={session}
          agents={agents}
          onChange={onPatch}
          disabled={pickerDisabled}
          allowedBackends={pinnedBackend ? [pinnedBackend] : undefined}
          defaultLabel={
            canClearToDefault
              ? defaultAgent
                ? t('newSession.defaultAgentNamed', { name: defaultAgent.name })
                : t('newSession.defaultAgent')
              : undefined
          }
          defaultRoute={defaultRoute}
          isDefaultRoute={inheritsDefault}
          compactMobile
        />
        {/* Chat hides the brand header, so mount the install nudge here too —
            IM-launched users often land straight in a chat. Renders only on iOS
            Safari + not-installed; null otherwise. */}
        <InstallHint />
        {/* Show Page toggle: swaps the chat surface for the session's Show Page
            (the header bar stays). First open initializes the page + prompts the
            agent. Pushed to the far right of the header row. */}
        <Button
          type="button"
          variant={showPageMode ? 'secondary' : 'ghost'}
          size="icon"
          onClick={onToggleShowPage}
          disabled={showPageBusy}
          aria-label={showPageMode ? t('chat.showPage.backToChat') : t('chat.showPage.open')}
          title={showPageMode ? t('chat.showPage.backToChat') : t('chat.showPage.open')}
          className="ml-auto size-7 shrink-0"
        >
          {showPageBusy ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : showPageMode ? (
            <MessageSquare className="size-3.5" />
          ) : (
            <AppWindow className="size-3.5" />
          )}
        </Button>
      </div>
    </div>
  );
};

interface TitleFieldProps {
  title: string | null;
  onCommit: (next: string | null) => void;
}

const TitleField: React.FC<TitleFieldProps> = ({ title, onCommit }) => {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title ?? '');
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setValue(title ?? '');
  }, [title]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        className="group inline-flex min-w-0 items-center gap-2 truncate text-left text-[16px] font-bold text-foreground hover:text-foreground"
      >
        <span className="truncate">{title || t('chat.untitled')}</span>
        <Pencil className="size-3.5 shrink-0 text-muted opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
    );
  }

  const commit = (next: string) => {
    const trimmed = next.trim();
    if (trimmed === (title ?? '')) {
      setEditing(false);
      return;
    }
    onCommit(trimmed || null);
    setEditing(false);
  };

  return (
    <Input
      ref={inputRef}
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => commit(value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') commit(value);
        if (e.key === 'Escape') {
          setValue(title ?? '');
          setEditing(false);
        }
      }}
      placeholder={t('chat.titlePlaceholder')}
      className="h-8 flex-1 px-2 text-[15px] font-bold"
    />
  );
};

interface TranscriptProps {
  messages: WorkbenchMessage[];
  session: WorkbenchSession;
  working: boolean;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  messageFontSize: number;
  onQuickReply: (messageId: string, choice: string) => boolean | void | Promise<boolean | void>;
}

const Transcript: React.FC<TranscriptProps> = ({
  messages,
  session,
  working,
  hasOlder,
  loadingOlder,
  onLoadOlder,
  messageFontSize,
  onQuickReply,
}) => {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  // ``true`` while the viewport is FOLLOWING the bottom (at/near it) — drives the
  // auto-follow of new content and hides the jump button. A ref, not state, so the
  // scroll handler + ResizeObserver read it without stale closures or extra renders.
  const pinnedRef = useRef(true);
  // While the user has scrolled UP to read history (not pinned), remember the
  // topmost row still in view and how far its top sits below the viewport top, so
  // any later content resize can put that exact row back where it was. This is a
  // manual scroll-anchor: iOS Safari still ships no CSS ``overflow-anchor``, so a
  // late-loading image would otherwise shift the page out from under the reader.
  const anchorRef = useRef<{ el: HTMLElement; top: number } | null>(null);
  const lastSessionRef = useRef<string | null>(null);
  const [showJump, setShowJump] = useState(false);
  const loadOlderRef = useRef(onLoadOlder);

  useEffect(() => {
    loadOlderRef.current = onLoadOlder;
  }, [onLoadOlder]);

  // The reply arrives atomically as a persisted ``result`` row (no streaming
  // card), so the thinking bubble shows for the whole gap between send and
  // reply. Hide it the moment the last row is a fresh agent terminal — a
  // successful ``result`` OR a failed ``error`` both end the turn.
  const lastIsAgentResult =
    messages.length > 0 &&
    messages[messages.length - 1].author === 'agent' &&
    (messages[messages.length - 1].type === 'result' || messages[messages.length - 1].type === 'error');
  const showThinking = working && !lastIsAgentResult;
  const empty = messages.length === 0 && !working;

  // Capture the topmost (partly) visible row as the restore anchor. Viewport-
  // relative rects keep this correct regardless of the scroll container's padding;
  // it breaks at the first visible row, so the common case (reading near the top of
  // the loaded window) is a couple of reads. Called from the scroll handler while
  // the user is reading history, so the anchor is always fresh when a resize lands.
  const captureAnchor = useCallback(() => {
    const el = scrollRef.current;
    const content = contentRef.current;
    if (!el || !content) return;
    const containerTop = el.getBoundingClientRect().top;
    for (const child of Array.from(content.children) as HTMLElement[]) {
      const rect = child.getBoundingClientRect();
      if (rect.bottom > containerTop) {
        anchorRef.current = { el: child, top: rect.top - containerTop };
        return;
      }
    }
    anchorRef.current = null;
  }, []);

  // Jump to the exact bottom and resume following. Instant, not smooth: a smooth
  // glide emits intermediate scroll events that would flip the pin off mid-flight
  // and, if content grows during the glide, land short of the true bottom.
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    pinnedRef.current = true;
    anchorRef.current = null;
    setShowJump(false);
  }, []);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    // Small tolerance keeps us "following" through sub-pixel rounding; the jump
    // button only appears once the user has scrolled up a clear distance.
    const pinned = distance < 80;
    pinnedRef.current = pinned;
    setShowJump(distance > 240);
    // Only track an anchor while reading history; following needs none (the bottom
    // is free to grow). Re-capturing here keeps it current as the user scrolls.
    if (pinned) anchorRef.current = null;
    else captureAnchor();
    if (hasOlder && !loadingOlder && el.scrollTop < 120) {
      loadOlderRef.current();
    }
  };

  // Open each session pinned to the latest message (instant, no animation) —
  // opening from the inbox should land on what just arrived.
  useEffect(() => {
    if (lastSessionRef.current === session.id) return;
    lastSessionRef.current = session.id;
    pinnedRef.current = true;
    anchorRef.current = null;
    setShowJump(false);
    const id = requestAnimationFrame(() => scrollToBottom());
    return () => cancelAnimationFrame(id);
  }, [session.id, scrollToBottom]);

  // The one place scroll position reacts to content size changes — two modes,
  // never conflated. (Conflating them WAS the bug: any resize while "at bottom"
  // forced scrollTop=scrollHeight, and the snap's own scroll event re-armed the
  // "at bottom" flag, so a history image loading as the user scrolled up kept
  // yanking them back to the latest message.)
  //   • following → stay pinned to the exact bottom as content grows (new message,
  //     thinking bubble, the latest message's own image finishing to load).
  //   • reading history → restore the saved anchor so the row under the reader's
  //     eyes stays fixed wherever the growth happened: an image above expands and
  //     the anchor moves down with it (scrollTop tracks it), while growth below the
  //     anchor leaves it alone.
  // ``[overflow-anchor:none]`` on the container hands anchoring entirely to us, so
  // behavior is identical on every browser instead of fighting Chrome/Firefox's
  // native anchoring; ResizeObserver delivers before paint, so the restore is
  // flicker-free. ``empty`` is in the deps so the observer (re)attaches when the
  // scroll container mounts after the empty state.
  useEffect(() => {
    const el = scrollRef.current;
    const content = contentRef.current;
    if (!el || !content) return;
    const ro = new ResizeObserver(() => {
      if (pinnedRef.current) {
        el.scrollTop = el.scrollHeight;
        return;
      }
      const anchor = anchorRef.current;
      if (!anchor || !anchor.el.isConnected) return;
      const currentTop = anchor.el.getBoundingClientRect().top - el.getBoundingClientRect().top;
      const delta = currentTop - anchor.top;
      // Sub-pixel rect noise would otherwise write scrollTop on every fire; only
      // correct a real (≥0.5px) drift so reading history stays perfectly still.
      if (Math.abs(delta) >= 0.5) el.scrollTop += delta;
    });
    ro.observe(content);
    return () => ro.disconnect();
  }, [empty]);

  if (empty) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center text-muted">
        <MessageSquare className="size-8 opacity-60" />
        <div className="text-[13px]">{t('chat.transcriptEmpty')}</div>
      </div>
    );
  }
  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={scrollRef} onScroll={handleScroll} className="min-h-0 flex-1 overflow-y-auto px-4 py-5 [overflow-anchor:none] md:px-8">
        <div ref={contentRef} className="mx-auto flex w-full max-w-[1080px] flex-col gap-3">
          {loadingOlder && (
            <div className="flex h-8 items-center justify-center text-muted">
              <Loader2 className="size-4 animate-spin" />
            </div>
          )}
          {messages.map((message) => (
            <MessageRow
              key={message.id}
              message={message}
              session={session}
              messageFontSize={messageFontSize}
              onQuickReply={onQuickReply}
            />
          ))}
          {showThinking && <ThinkingBubble session={session} />}
        </div>
      </div>
      {/* Jump-to-latest: appears after scrolling up a clear distance, returns to
          the bottom on click. Centered just above the compose bar. */}
      {showJump && (
        <Button
          type="button"
          variant="secondary"
          size="icon"
          onClick={() => scrollToBottom()}
          aria-label={t('chat.scrollToBottom')}
          className="absolute bottom-3 left-1/2 size-9 -translate-x-1/2 rounded-full border-border-strong shadow-lg"
        >
          <ChevronDown className="size-4" />
        </Button>
      )}
    </div>
  );
};


// Small role avatar — a tinted rounded square with a lucide glyph, shown on the
// header line above a left-aligned message bubble (IM layout). Kept on its own
// line with the name so it never eats into the bubble's usable width.
const TONE_AVATAR: Record<'mint' | 'cyan' | 'gold' | 'muted', string> = {
  mint: 'border-mint/30 bg-mint/[0.13] text-mint',
  cyan: 'border-cyan/30 bg-cyan/[0.13] text-cyan',
  gold: 'border-gold/30 bg-gold/[0.13] text-gold',
  muted: 'border-border-strong bg-foreground/[0.06] text-muted',
};
const RoleAvatar: React.FC<{ tone: keyof typeof TONE_AVATAR; children: React.ReactNode }> = ({ tone, children }) => (
  <span className={clsx('flex size-6 shrink-0 items-center justify-center rounded-lg border [&_svg]:size-3.5', TONE_AVATAR[tone])}>
    {children}
  </span>
);

// Shown while a turn is in flight but the reply hasn't landed yet — a left
// agent bubble with three dots that fade in sequence (``.vr-typing-dot``
// keyframes in index.css), so the user gets immediate feedback a reply is
// coming (feedback #1).
const ThinkingBubble: React.FC<{ session: WorkbenchSession }> = ({ session }) => {
  const { t } = useTranslation();
  return (
    <div className="flex w-full justify-start">
      <div className="group/message flex max-w-[min(92%,860px)] flex-col items-start gap-1">
        <div className="flex items-center gap-2 px-0.5">
          <RoleAvatar tone="mint"><Bot /></RoleAvatar>
          <span className="text-[11px] font-medium text-muted">{session.agent_name || t('chat.thinking')}</span>
        </div>
        <div className="w-fit rounded-2xl rounded-tl-md border border-mint/25 bg-mint/[0.09] px-3.5 py-2.5">
          <div className="flex items-center gap-1 py-0.5">
            <span className="vr-typing-dot size-1.5 rounded-full bg-mint" />
            <span className="vr-typing-dot size-1.5 rounded-full bg-mint [animation-delay:0.2s]" />
            <span className="vr-typing-dot size-1.5 rounded-full bg-mint [animation-delay:0.4s]" />
          </div>
        </div>
      </div>
    </div>
  );
};

// Maps a harness trigger kind (the ``author_name`` on a source='harness' row)
// to a friendly provenance label. Distinguishes Task vs Watch per the spec; a
// finer kind (webhook) gets its own label, anything else falls back.
const harnessLabel = (kind: string | null | undefined, t: (k: string) => string): string => {
  switch (kind) {
    case 'watch':
      return t('chat.source.watch');
    case 'webhook':
      return t('chat.source.webhook');
    case 'scheduled':
    case 'task_run':
      return t('chat.source.scheduled');
    default:
      return t('chat.source.harness');
  }
};

type MessageRowProps = {
  message: WorkbenchMessage;
  session: WorkbenchSession;
  messageFontSize: number;
  onQuickReply?: (messageId: string, choice: string) => boolean | void | Promise<boolean | void>;
};

// Memoized so a transcript re-render that doesn't touch THIS row — the scroll
// handler's showJump toggle, the working/thinking state, a sibling message
// arriving — skips it entirely. Without this, every such re-render re-runs
// <Markdown>, which (via react-markdown) remounts the row's <img>s; a remounted
// image is re-decoded, which is what flickers the bubble on iOS Safari while
// scrolling. The props are referentially stable per row (the message/session
// objects only change when that row's data does, and onQuickReply is a
// useCallback), so the default shallow compare is correct here.
const MessageRow = memo(function MessageRow({ message, session, messageFontSize, onQuickReply }: MessageRowProps) {
  const { t } = useTranslation();
  // Harness rows are collapsed by default; this tracks the per-row expand state.
  const [expanded, setExpanded] = useState(false);

  // A notify row is a turn-terminal marker (agent run that failed/stopped
  // without a result) — a compact status pill, not an answer.
  const isNotify = message.type === 'notify';
  const isAgent = !isNotify && message.author === 'agent';
  const isSystem = !isNotify && message.author === 'system';
  // A harness-origin row is a user-role prompt the human didn't type (scheduled
  // task / watch / webhook); collapsed by default so it doesn't dominate.
  const isHarness = !isNotify && !isAgent && !isSystem && message.source === 'harness';
  const isUser = !isNotify && !isAgent && !isSystem && !isHarness;
  const messageFontStyle = { fontSize: `${normalizeChatMessageFontSize(messageFontSize)}px` };

  // User-uploaded attachments ride in ``content.attachments`` (agent-reply media
  // is rewritten inline into the text instead, handled by the Markdown renderer).
  const rawAttachments = (message.content as { attachments?: Array<Record<string, unknown>> })?.attachments;
  const messageAttachments = Array.isArray(rawAttachments) ? rawAttachments : [];
  const attachmentsNode = messageAttachments.length > 0 ? (
    <div className="mt-2 flex flex-col gap-2">
      {messageAttachments.map((att, i) => {
        const url = String(att?.url || '');
        if (!url) return null;
        // Only inline-render images served from our own media proxy; a non-proxy
        // url falls back to a click-through FileCard so it can't auto-fetch a
        // remote host.
        const isImage =
          (att?.kind === 'image' || String(att?.mime || '').startsWith('image/')) && isProxyMediaUrl(url);
        // Server-supplied pixel size (added at upload time) reserves the box so a
        // freshly-loaded attachment never shifts the transcript.
        const w = typeof att?.width === 'number' ? att.width : undefined;
        const h = typeof att?.height === 'number' ? att.height : undefined;
        return isImage ? (
          <ChatImage key={i} src={url} alt={typeof att?.name === 'string' ? att.name : ''} width={w} height={h} />
        ) : (
          <FileCard key={i} href={url}>
            {typeof att?.name === 'string' ? att.name : 'file'}
          </FileCard>
        );
      })}
    </div>
  ) : null;

  // Agent quick-reply buttons: the options AND the chosen answer both live on
  // THIS message's ``content`` (parsed server-side; the chosen answer recorded on
  // the same message is the single source of truth for the lock — no correlating
  // a separate user reply). IM channels render native buttons from the same parse.
  const qr = isAgent
    ? (message.content as { quick_replies?: unknown; quick_reply_chosen?: unknown } | null)
    : null;
  const quickReplyOptions = Array.isArray(qr?.quick_replies)
    ? qr!.quick_replies.filter((x): x is string => typeof x === 'string' && x.length > 0)
    : [];
  const quickReplyChosen = typeof qr?.quick_reply_chosen === 'string' ? qr.quick_reply_chosen : null;
  const quickRepliesNode =
    quickReplyOptions.length > 0 && onQuickReply ? (
      <QuickReplies
        options={quickReplyOptions}
        chosen={quickReplyChosen}
        onChoose={(choice) => onQuickReply(message.id, choice)}
      />
    ) : null;

  // Agent / system replies AND the user's own messages render as markdown (users
  // routinely type lists / code / **emphasis** and expect it formatted). Only
  // Every message body renders as Markdown — including the expanded harness row
  // (scheduled task / watch / webhook prompt), which used to stay verbatim.
  // Harness prompts and the user's own messages keep soft breaks so their
  // original line breaks stay visible (a harness prompt often mixes authored
  // Markdown with line-oriented waiter output); agent/system replies are
  // authored Markdown and must not get stray hard breaks.
  const bodyNode = message.text ? (
    <Markdown
      content={message.text}
      softBreaks={isUser || isHarness}
      references={(message.content as { references?: MentionReference[] } | null)?.references}
      className="vr-markdown--inherit-size"
    />
  ) : messageAttachments.length === 0 ? (
    <div className="text-[13px] text-muted">—</div>
  ) : null;

  // Timestamp is metadata, not content: hidden by default, revealed while the
  // pointer is over the message OR focus moves into it (a keyboard user tabbing
  // to a link/button inside; doesn't add a tab stop on its own). The column
  // carries a NAMED group (``group/message``) so this reveal can't collide with
  // the unnamed ``group-hover`` ChatImage uses for its own overlay button.
  // Coarse pointers (touch) have no hover, so keep it always visible there.
  const time = (
    <span className="px-1 font-mono text-[10px] text-muted opacity-0 transition-opacity duration-150 group-hover/message:opacity-100 group-focus-within/message:opacity-100 pointer-coarse:opacity-100">
      {formatLocalDateTime(message.created_at)}
    </span>
  );

  // ----- Notify: compact gold pill, left-aligned (a status marker) -----
  if (isNotify) {
    return (
      <div className="flex w-full justify-start">
        <div className="group/message flex max-w-[min(92%,860px)] flex-col items-start gap-1">
          <div className="inline-flex w-fit max-w-full items-start gap-1.5 rounded-2xl rounded-tl-md border border-gold/30 bg-gold/[0.08] px-3 py-1.5 text-[12px] text-gold">
            <Bell className="mt-px size-3 shrink-0" />
            <span className="min-w-0 break-words">
              <span className="font-semibold">{t('chat.notifyLabel')}</span>
              {message.text && <span className="font-normal text-gold/80"> · {message.text}</span>}
            </span>
          </div>
          {time}
        </div>
      </div>
    );
  }

  // ----- User: right-aligned neutral bubble (kept distinct from agent mint) ---
  if (isUser) {
    return (
      <div className="flex w-full justify-end">
        <div className="group/message flex max-w-[min(92%,860px)] flex-col items-end gap-1">
          <div
            className="w-fit min-w-0 max-w-full rounded-2xl rounded-tr-md border border-border-strong bg-foreground/[0.06] px-3.5 py-2.5 leading-relaxed [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_table]:w-full"
            style={messageFontStyle}
          >
            {bodyNode}
            {attachmentsNode}
          </div>
          {time}
        </div>
      </div>
    );
  }

  // ----- Harness: avatar+type header, then a narrow chip that expands -----
  if (isHarness) {
    return (
      <div className="flex w-full justify-start">
        <div className="group/message flex max-w-[min(92%,860px)] flex-col items-start gap-1">
          <div className="flex items-center gap-2 px-0.5">
            <RoleAvatar tone="cyan"><Clock /></RoleAvatar>
            <span className="text-[11px] font-medium text-cyan">{harnessLabel(message.author_name, t)}</span>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="h-auto max-w-[360px] justify-start gap-2 rounded-xl rounded-tl-md border border-dashed border-cyan/40 bg-cyan/[0.05] px-3 py-1.5 hover:bg-cyan/[0.10]"
          >
            <span className="min-w-0 truncate text-[12px] text-muted">
              {expanded ? t('chat.collapse') : message.text?.trim() || '—'}
            </span>
            <ChevronDown className={clsx('size-3.5 shrink-0 text-muted transition-transform', expanded && 'rotate-180')} />
          </Button>
          {expanded && (
            <div className="w-fit max-w-full rounded-2xl rounded-tl-md border border-cyan/25 bg-cyan/[0.08] px-3.5 py-2.5 text-[13px] leading-relaxed">
              {bodyNode}
              {attachmentsNode}
            </div>
          )}
          {time}
        </div>
      </div>
    );
  }

  // ----- Agent / system: left-aligned bubble with avatar + name header -----
  const name = isAgent ? session.agent_name || message.author_name : message.author_name;
  return (
    <div className="flex w-full justify-start">
      <div className="group/message flex max-w-[min(92%,860px)] flex-col items-start gap-1">
        <div className="flex items-center gap-2 px-0.5">
          <RoleAvatar tone={isAgent ? 'mint' : 'muted'}>{isAgent ? <Bot /> : <Info />}</RoleAvatar>
          {name && <span className="text-[11px] font-medium text-muted">{name}</span>}
        </div>
        <div
          className={clsx(
            'w-fit min-w-0 max-w-full rounded-2xl rounded-tl-md border px-3.5 py-2.5 leading-relaxed [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_table]:w-full',
            isAgent ? 'border-mint/25 bg-mint/[0.09]' : 'border-border bg-foreground/[0.03]',
          )}
          style={messageFontStyle}
        >
          {bodyNode}
          {attachmentsNode}
        </div>
        {quickRepliesNode}
        {time}
      </div>
    </div>
  );
});

const ChatMissing: React.FC<{ onBack: () => void }> = ({ onBack }) => {
  const { t } = useTranslation();
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 py-8">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-[12px] text-cyan hover:underline"
      >
        <ArrowLeft className="size-3.5" />
        {t('chat.back')}
      </button>
      <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
        {t('chat.missingSessionId')}
      </div>
    </div>
  );
};
