import { Fragment, forwardRef, memo, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Activity, ArrowLeft, ArrowRight, ArrowUpRight, Bell, Bot, ChevronDown, ChevronRight, Clock, Eye, GitFork, Image as ImageIcon, Info, Loader2, MapPin, MessageSquare, MessageSquareQuote, Pencil, Terminal, Undo2, UploadCloud, X } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { selectApiErrorFields } from '../../context/apiErrorParse';
import { useToast } from '../../context/ToastContext';
import { useWorkbenchInbox } from '../../context/WorkbenchInboxContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { useRegisterComposerTarget, type ComposerInsertTarget } from '../../context/ComposerBridgeContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import type { SessionActivityItemKind, SessionActivityState, SessionRuntimeState, VaultRequest, VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { apiFetch } from '../../lib/apiFetch';
import { readChatViewMode, writeChatViewMode } from '../../lib/chatViewMemory';
import { normalizeChatMessageFontSize } from '../../lib/chatDisplay';
import { setConfigField } from '../../lib/configMutations';
import { annotationStandIn, annotationTitleKey, readAnnotationView } from '../../lib/annotationView';
import { isTerminalAgentMessage, isTranscriptMessage, shouldRefreshAgentActivityForMessage } from '../../lib/chatMessageTypes';
import { chatRowKind, drawsEmptyBodyPlaceholder, isAgentAuthored } from '../../lib/chatRowKind';
import { useIosKeyboardInset } from '../../lib/useIosKeyboardInset';
import { isProxyMediaUrl } from '../../lib/mediaProxy';
import {
  isVaultApprovalRequest,
  placeVaultProvisionRequests,
} from '../../lib/vaultRequestPlacement';
import { editorPath, type ShowPageLinkInfo } from '../../lib/showPageLinks';
import {
  showPageHeaderAccess,
  showPageRestoreAccessDecision,
  type ShowPageAccess,
  type ShowPageAccessProbe,
} from '../../lib/showPageAccess';
import { showPageEmbeddedPath } from '../../apps/showPageAvatar';
import { downloadFile, fileMeta } from '../../lib/filesApi';
import { isEditableFile, isEditableMeta, previewOverlayKind } from '../../lib/filePreview';
import { recentPathLabel } from '../../lib/editorRecents';
import type { LocalFileLinkTarget } from '../../lib/localFileLinks';
import { formatLocalDateTime, formatRelativeTime } from '../../lib/relativeTime';
import { canMarkConversationRead, usePageActive } from '../../lib/pageActivity';
import { useRouteSurfaceActive, useRouteSurfaceWindowEvent } from '../../lib/routeSurfaceActivity';
import { isDesktopViewport, useIsDesktop } from '../../lib/useIsDesktop';
import { resultFooterParts } from '../../lib/resultFooter';
import {
  activityItemKind,
  activityKindI18nKey,
  harnessNavPath,
  isQueuedRun,
  resolveActivityLabel,
  sortBackgroundActivities,
} from '../../lib/backgroundActivity';
import {
  chatTriggerLink,
  harnessChipLabelKey,
  isVaultCallback,
  needsHarnessProvenanceReconcile,
  vaultCallbackStatusKey,
} from '../../lib/chatTrigger';
import { AnnotationMessage } from './AnnotationMessage';
import { AGENT_BUBBLE, SYSTEM_BUBBLE, USER_BUBBLE } from './chatBubble';
import { RoleAvatar } from './RoleAvatar';
import { useFileDrop } from '../../lib/useFileDrop';
import { quoteText } from '../../lib/quoteText';
import {
  isTranscriptWindowDisjoint,
  mergeById,
  insertMessageOrdered,
  reconcileWorkbenchClaimedDeliveries,
} from '../../lib/transcriptOrder';
import { pickScrollAnchor } from '../../lib/transcriptScrollAnchor';
import { AgentRoutePicker } from './AgentRoutePicker';
import {
  archiveSessionShortcutLabel,
  isArchiveSessionChord,
  isArchiveSessionKeydown,
} from './chatShortcuts';
import { bindFrameChord } from '../apps/windowChords';
import {
  MobileChatSessionActionMenu,
  type SessionActionDescriptor,
} from './sessionActions';
import { useSessionActions } from './useSessionActions';
import { ShowPageShareControl } from './ShowPageShareControl';
import { ShowPageAnnotateControl } from './ShowPageAnnotateControl';
import { ShowPageLaunchControl } from './ShowPageLaunchControl';
import { useShowPageAnnotation, type AnnotationBridge } from './useShowPageAnnotation';
import { SelectionQuoteToolbar } from './SelectionQuoteToolbar';
import {
  isSessionArchivedConflict,
  isSessionArchivedError,
  isSessionReadOnly,
  isShowPageActive,
  markSessionArchived,
  sessionReadOnlyReason,
  showPageControlActions,
  transcriptSelectionActions,
  type SessionReadOnlyReason,
} from './sessionArchived';
import {
  bySessionWriteGroup,
  commitSessionRowWrite,
  createSessionRowRefreshGate,
  recordSessionRowWrite,
  releaseSessionRowWrite,
  sessionRowWithBootstrapFallback,
  sessionWriteStandsAlone,
  useChatSessionRow,
  type SessionRowRefreshGate,
  type SessionWriteGroup,
} from './sessionRowRefresh';
import { chatSessionViewState } from './chatSessionViewState';
import { InstallHint } from '../InstallHint';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { ChatImage } from '../ui/chat-image';
import { FileCard } from '../ui/file-card';
import { ImageViewerProvider } from '../ui/image-viewer';
import { FileViewerProvider } from '../ui/file-viewer';
import { useFileViewer } from '../ui/file-viewer-context';
import { Input } from '../ui/input';
import { Markdown } from '../ui/markdown';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { VaultApprovalFloat, VaultChatRequests } from '../ui/vault-chat-requests';
import { VaultProvisionDialogProvider, VaultRequestCard } from '../ui/vault-request-card';
import { StatusPill } from '../visual';
import { usePendingVaultRequests } from '../../lib/usePendingVaultRequests';
import { useCoalescedWrite } from '../../lib/useCoalescedWrite';
import { hasInAppBackEntry } from '../../lib/navigationHistory';
import { Composer, type ComposerAttachment, type ComposerHandle, type ComposerProps } from './Composer';
import type { MentionReference } from '../../lib/mentions';
import { QuickReplies } from './QuickReplies';
import { ActivityCard, ActivityChip } from './AgentActivityGroup';
import {
  activityGroupsForForeground,
  activityRowFromMessage,
  groupFromWire,
  initialLiveActivity,
  isActivityMessageType,
  liveActivityReducer,
  shouldShowRunningCard,
  type ActivityForeground,
  type ActivityGroup,
  type ActivityRow,
  type LiveActivityEvent,
  type LiveActivityState,
  type TurnActivityGroupWire,
} from '../../lib/agentActivity';
import { errorMessage } from '@/lib/errorMessage';
import { pendingInitialMessageHandoff } from '@/lib/chatInitialMessage';
import { sessionAgentDisplayName } from './sessionAgentName';

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
const ACTIVITY_RECONCILE_INTERVAL_MS = 10 * 1000;

const emptyRuntimeState = (): SessionRuntimeState => ({
  in_flight: false,
  foreground: 'idle',
  native_turn_started: false,
  pending_input_count: 0,
  background_activities: [],
  pending_activity_output_count: 0,
  connection: 'unknown',
});

// Bounded retained transcript window: cap how many message rows stay mounted so a
// long streaming session (or a deep upward scroll) doesn't keep thousands of full
// react-markdown subtrees in the DOM. The trimmed rows stay in SQLite and page
// back in on demand (scroll up re-fetches older; the jump-to-latest button reloads
// the live tail). Generous enough that normal chats never hit it. Enforced at two
// kinds of site: while following the tail, ``appendMessage`` / ``reconcile`` drop
// the OLDEST overflow (above the pinned viewport); while the reader is scrolled up,
// the ingest points (``onMessageNew`` / ``reconcile``) and ``loadOlderMessages``
// detach the live tail (historical-window) instead of growing the DOM with rows
// below the viewport.
const MAX_RETAINED_MESSAGES = 300;

// How close to the top of the loaded window counts as "the reader is asking for
// older history". Handed to the older-page IntersectionObserver as a root margin,
// so the browser evaluates it continuously as a BAND the reader can sit inside —
// not as a threshold some event has to be caught crossing.
const OLDER_TRIGGER_BAND_PX = 120;

// Display label for the archive chord (⇧⌘D / Ctrl+Shift+D). Resolved once at
// module load — the platform can't change mid-session — and shown as the archive
// row's hint so the shortcut is discoverable instead of folklore.
const ARCHIVE_SHORTCUT_LABEL = archiveSessionShortcutLabel();

// One session write. The header applies an edit optimistically, so the request
// that follows carries the chat it was made for: it is recorded under that
// session's id (the URL may already be on another chat by the time it flushes)
// and carries that chat's read-ordering gate, which is replaced on navigation.
// It also carries which of the row's field groups it belongs to, because the
// optimistic record it commits against is per group — the writer key alone says
// that to the writer, not to the sender it hands the payload to.
type SessionPatchWrite = {
  changes: Partial<WorkbenchSession>;
  gate: SessionRowRefreshGate;
  group: SessionWriteGroup;
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
  const { showToast } = useToast();
  const location = useLocation();
  // Deep-link target: the search palette routes to /chat/<session>?msg=<message>
  // (P3 contract). When set, the jump effect below scrolls to + briefly
  // highlights that message, fetching a centered window around it if it isn't
  // in the loaded transcript. The param is cleared after handling so a
  // re-render / visibility gap-recovery can't re-trigger the jump.
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkMessageId = searchParams.get('msg');
  // A "show me the chat" navigation carries ?view=chat (see sessionChatPath({ showChat:
  // true })) — a general signal that this navigation must leave Show Page mode.
  const showChatSignal = searchParams.get('view') === 'chat';
  const api = useApi();
  const {
    capabilities,
  } = useInstanceAuthorization();
  const [sessionCanChat, setSessionCanChat] = useState(false);
  const canChat = capabilities.can_chat && sessionCanChat;
  const canManageShowPageAsInstance = capabilities.can_use_show_pages;
  const [showPageAccessResult, setShowPageAccessResult] = useState<{
    sessionId: string;
    probe: ShowPageAccessProbe;
  } | null>(null);
  const showPageAccessProbeGenerationRef = useRef(0);
  const currentShowPageAccessProbe = canManageShowPageAsInstance
    && showPageAccessResult?.sessionId === sessionId
    ? showPageAccessResult?.probe ?? null
    : null;
  const showPageAccess = currentShowPageAccessProbe?.status === 'granted'
    ? currentShowPageAccessProbe.access
    : null;
  const showPageRestoreAccess = showPageRestoreAccessDecision(
    canManageShowPageAsInstance,
    currentShowPageAccessProbe,
  );
  const { canOpen: canOpenShowPage, canManage: canManageShowPage } = showPageHeaderAccess(
    canManageShowPageAsInstance,
    showPageAccess,
  );
  // This session's own unread state, plus the mark-read write. Neither needs the
  // feed page, so this component never asks for one; whether the document loads
  // one at all is decided by the sidebar, which mounts on desktop only.
  const { unreadBySession, markRead: markInboxRead } = useWorkbenchInbox({ feed: false });
  const { focusedId: foregroundAppWindowId, focusCanvas } = useWindowManager();
  const isDesktop = useIsDesktop();
  const routeSurfaceActive = useRouteSurfaceActive();
  const pageActive = usePageActive();
  // The mobile chat surface is a fixed full-screen flex column; this keeps the
  // composer glued to the iOS keyboard (settle-then-correct; see the hook).
  const chatSurfaceRef = useRef<HTMLDivElement>(null);
  useIosKeyboardInset(chatSurfaceRef);

  // Loaded session (null while bootstrapping — ChatPage renders a loader until
  // it's set). Lifted above the composer bridge + show-page logic that gate on it.
  // Two ways to move it, by provenance: ``installFromServer`` for anything the
  // server sent (an open write is re-applied on top of it), ``applyLocal`` for
  // this document's own optimistic state. The raw setter is deliberately out of
  // scope — see ``useChatSessionRow``.
  const { session, installFromServer: installServerSession, applyLocal: applyLocalSession } =
    useChatSessionRow<WorkbenchSession>();
  const sessionRowRefreshGateRef = useRef(createSessionRowRefreshGate());
  // Archive is terminal: an archived transcript stays fully readable (search's
  // "include archived" opt-in links straight here) but every mutation is refused
  // server-side, so the chat renders read-only — no composer, no rename, no
  // re-route, no transcript control that would write to the session, and no Show
  // Page controls (archive takes the page offline and refuses to create one, so
  // Visualize and Share could only fail; see showPageControlActions).
  //
  // A ``visibility === 'system'`` session is read-only for a DIFFERENT reason and to
  // the same depth: the runtime owns the row (the workspace-notifications session the
  // Inbox links to) and the messages POST answers 403 ``reserved_session``. The reason
  // is carried alongside because only the COPY differs — nothing here may call such a
  // session archived.
  const readOnlyReason = sessionReadOnlyReason(session);
  const readOnly = isSessionReadOnly(session);
  const writable = canChat && !readOnly;
  const metadataWritable = sessionCanChat && !readOnly;
  // The shared Messaging settings use this same capability boundary: Editors
  // can persist display preferences, while Viewers can only observe them.
  const canEditAgentActivityVisibility = capabilities.can_chat || capabilities.can_use_system;

  // Chat-page-wide drag-and-drop: dropping files anywhere over the chat surface
  // (not just the input row) stages them on the composer via its imperative
  // handle. Desktop-only in practice — touch fires no drag events — and disabled
  // until a session exists (the upload endpoint is session-scoped) or when the
  // session is archived (staged files could never be sent).
  const composerRef = useRef<ComposerHandle>(null);
  const { dragging: fileDragging, handlers: fileDropHandlers } = useFileDrop(
    (files) => composerRef.current?.addFiles(files),
    { disabled: !sessionId || !writable },
  );

  // Show Page toggle: swap the chat surface (transcript + composer, NOT the
  // header bar) for this session's Show Page in an iframe, and back. Declared
  // before the composer bridge target, which depends on showPageMode.
  const [showPageMode, setShowPageMode] = useState(false);
  const [showPageBusy, setShowPageBusy] = useState(false);
  const [showPageViewResolved, setShowPageViewResolved] = useState(false);
  // One authority invalidates an in-flight restore/open when the user explicitly
  // chooses Chat (including a same-session ?view=chat navigation).
  const showPageRequestRef = useRef(0);
  useEffect(
    () => () => {
      // External launches resolve after an async ensure. Once this page has
      // unmounted, none of those prepared actions may still open or pin it.
      showPageRequestRef.current += 1;
    },
    [],
  );
  const showPageRestoreAttemptRef = useRef<string | null>(null);
  const selectChatView = useCallback((sid: string, remember: boolean) => {
    showPageRequestRef.current += 1;
    setShowPageMode(false);
    setShowPageBusy(false);
    if (remember) writeChatViewMode(sid, 'chat');
  }, []);
  // Sessions whose first-open visualize prompt failed to send — retry it on the
  // next toggle (the page row already exists, so `existed` alone won't re-prompt).
  const showPagePromptRetryRef = useRef<Set<string>>(new Set());
  const [showPageUrl, setShowPageUrl] = useState<string | null>(null);
  // Show Page mode as the page actually RENDERS it. A read-only (archived)
  // session withdraws the whole Show Page action cluster — Visualize, Share and
  // the annotation control (see showPageControlActions) — and back-to-chat is
  // that same Visualize button, so a tab that was ALREADY framing the page when
  // the session went archived (the stale-tab 409-convergence path) would
  // otherwise be stranded on a page it can no longer leave, and which archive
  // already forced offline. Fall back to the transcript instead.
  //
  // Derived rather than an effect on purpose: the fallback lands in the SAME
  // render that flips ``readOnly``. An effect would first commit one frame with
  // the chat surface still hidden and the iframe already gone — a blank chat.
  const showPageAccessDenied = showPageRestoreAccess === 'deny';
  const showPageActive = isShowPageActive(readOnly, showPageMode, showPageAccessDenied);
  // The voice chord belongs to the active Chat page, not to whichever child
  // happens to hold focus. Capture lets it win before the rich editor handles a
  // user-configured chord such as Ctrl+Z. Starting remains limited to a writable,
  // visible Chat surface; once recording, Composer still accepts the chord as
  // Finish after focus moves elsewhere in this document.
  const voiceShortcutCanStart = routeSurfaceActive && pageActive && writable && !showPageActive;
  useRouteSurfaceWindowEvent('keydown', (event) => {
    composerRef.current?.handleVoiceShortcut(event, voiceShortcutCanStart);
  }, true, true);
  // True while the share popover is open. The popover floats over the Show Page
  // iframe; making the iframe inert lets an outside tap there reach the parent
  // document so the (non-modal) popover dismisses, without modal-blocking the
  // sibling header buttons (which would then need two taps).
  const [shareOpen, setShareOpen] = useState(false);
  // True while the mobile annotation mode-picker popover is open. Like the share
  // popover it floats over the iframe, so it also makes the iframe inert.
  const [annotateOpen, setAnnotateOpen] = useState(false);
  // postMessage bridge to the Show Page iframe: sends annotation control
  // messages and derives the header control's state from the overlay's state
  // broadcasts (contract §3). Keyed off showPageMode+showPageUrl (null while the
  // iframe is hidden) so leaving Show Page mode resets state to unknown — else
  // closing then reopening the SAME session's page (showPageUrl unchanged) would
  // show the stale enabled/mode and could send control messages to the freshly
  // remounted overlay before it rebroadcasts. Re-points reset via the URL change.
  const annotation = useShowPageAnnotation(showPageActive ? showPageUrl : null);
  useEffect(() => {
    const sid = sessionId;
    if (!sid || !showPageAccessDenied) return;
    // The derived showPageActive value already withdrew the iframe in this
    // render. Persist the fallback and invalidate any in-flight ensure so a
    // late response cannot re-open content after access was revoked.
    showPageRestoreAttemptRef.current = sid;
    selectChatView(sid, true);
    setShowPageUrl(null);
  }, [selectChatView, sessionId, showPageAccessDenied]);
  const probeShowPageAccess = useCallback(async (targetSessionId: string) => {
    const generation = ++showPageAccessProbeGenerationRef.current;
    try {
      const nextProbe = await api.probeShowPageAccess(targetSessionId);
      if (generation !== showPageAccessProbeGenerationRef.current) return;
      setShowPageAccessResult({ sessionId: targetSessionId, probe: nextProbe });
    } catch {
      if (generation !== showPageAccessProbeGenerationRef.current) return;
      setShowPageAccessResult({
        sessionId: targetSessionId,
        probe: { status: 'error', access: null },
      });
    }
  }, [api]);
  useEffect(() => {
    if (!sessionId || !canManageShowPageAsInstance) return undefined;
    void probeShowPageAccess(sessionId);
    return () => {
      showPageAccessProbeGenerationRef.current += 1;
    };
  }, [canManageShowPageAsInstance, probeShowPageAccess, sessionId]);
  // The mounted Show Page frame, so parent-level chords can also be bound inside
  // its document (see the ⌘⇧D effect). Stable callback + ref, never state: a ref
  // callback that set state would re-create itself on every commit and re-attach
  // forever. Effects run after refs are attached, so the frame is here in time.
  const showPageFrameRef = useRef<HTMLIFrameElement | null>(null);
  const annotationSetIframe = annotation.setIframe;
  const setShowPageIframe = useCallback<React.RefCallback<HTMLIFrameElement>>(
    (node) => {
      showPageFrameRef.current = node;
      annotationSetIframe(node);
    },
    [annotationSetIframe],
  );
  useEffect(() => {
    // ChatPage is reused across :sessionId. Clear the previous frame immediately;
    // once the new session row loads, the restore effect below applies its own
    // remembered view through the same open path as a user click.
    showPageRestoreAttemptRef.current = null;
    setShowPageViewResolved(false);
    selectChatView(sessionId ?? '', false);
    setShowPageUrl(null);
  }, [selectChatView, sessionId]);

  // Honor the ?view=chat "show me the chat" signal ONCE: leave Show Page mode and strip
  // the param. This makes the intent work even for a same-session jump (where the
  // :sessionId path doesn't change, so the reset-on-sessionId effect above never fires);
  // stripping it (like the ?msg jump below) keeps it a no-op on every render afterwards.
  useEffect(() => {
    if (!showChatSignal) return;
    const sid = sessionId ?? '';
    showPageRestoreAttemptRef.current = sid || null;
    selectChatView(sid, true);
    setShowPageViewResolved(true);
    const next = new URLSearchParams(window.location.search);
    next.delete('view');
    setSearchParams(next, { replace: true });
  }, [selectChatView, sessionId, showChatSignal, setSearchParams]);

  // Publish this chat's composer to the ComposerBridge so the sidebar's
  // "reference this session" action can insert a #<session> mention into the
  // open chat's input.
  const insertSessionReference = useCallback(
    (refSessionId: string, title?: string | null) =>
      composerRef.current?.insertSessionReference(refSessionId, title),
    [],
  );

  // Chat-selection toolbar actions. "Quote" appends the quoted selection to the
  // current composer; "Ask in a new session" forks this session and seeds the
  // fork's draft with the same quote, then navigates to it.
  const quoteSelectionToComposer = useCallback(
    // Trailing space so the user's next typed text is separated from the quote.
    (text: string) => composerRef.current?.appendText(quoteText(text) + ' '),
    [],
  );
  const askInNewSession = useCallback(
    async (text: string) => {
      if (!sessionId) return;
      try {
        const forked = await api.forkSession(sessionId);
        if (!forked?.id) return;
        // setSessionDraft returns {ok:false} for a non-OK response (it doesn't
        // throw), so check it before navigating — don't strand the user in a
        // fork with an empty composer and a lost selection.
        // Trailing space so typing continues separated from the quote.
        const saved = await api.setSessionDraft(forked.id, quoteText(text) + ' ');
        if (!saved?.ok) {
          showToast(t('chat.selection.askFailed'), 'error');
          return;
        }
        navigate(`/chat/${encodeURIComponent(forked.id)}`);
      } catch {
        showToast(t('chat.selection.askFailed'), 'error');
      }
    },
    [sessionId, api, navigate, showToast, t],
  );
  // Null target hides that sidebar action unless the composer is actually
  // mounted + insertable: a chat is open (sessionId), its session has loaded
  // (before that ChatPage shows a loader — the composer isn't rendered yet), and
  // the Show Page iframe hasn't replaced the composer. Otherwise an insert would
  // silently no-op against a null composerRef. An archived (read-only) chat is
  // also not insertable — its composer is disabled, so the insert would land in a
  // box that can never be sent.
  const composerTarget = useMemo<ComposerInsertTarget | null>(
    () =>
      sessionId && session != null && !showPageActive && writable
        ? { sessionId, insertSessionReference }
        : null,
    [sessionId, session, showPageActive, writable, insertSessionReference],
  );
  useRegisterComposerTarget(composerTarget);

  // Pending vault requests for this session. Provision requests are attached to
  // the Agent reply that announced them; access/sign retain the approval flow.
  const { requests: vaultRequests, refresh: refreshVaultRequests } = usePendingVaultRequests(sessionId ?? '');
  // All pending approval (access/sign) requests for this session govern the
  // float's mount and dialog lifetime. Provision requests never enter it.
  const pendingApprovals = useMemo(
    () => vaultRequests.filter(isVaultApprovalRequest),
    [vaultRequests],
  );
  const [offscreenApprovals, setOffscreenApprovals] = useState<VaultRequest[]>([]);

  // Back returns to the page the user came from, not a hardcoded inbox. The
  // history index is the source of truth: replaceState can assign a non-default
  // location key without creating an entry that can actually be popped.
  const goBack = useCallback(() => {
    if (hasInAppBackEntry(window.history.state)) navigate(-1);
    else navigate('/inbox');
  }, [navigate]);

  const [agents, setAgents] = useState<VibeAgentBrief[]>([]);
  const [defaultAgentName, setDefaultAgentName] = useState<string | null>(null);
  const [messages, setMessages] = useState<WorkbenchMessage[]>([]);
  // A Session row can arrive from lightweight SSE recovery before the combined
  // bootstrap installs this route's messages. Keep that row from exposing the
  // reset empty array as a real empty transcript. The marker survives same-route
  // reconnect/auth refreshes so an already visible chat stays visible.
  const [hydratedTranscriptSessionId, setHydratedTranscriptSessionId] = useState<string | null>(null);
  const [failedBootstrapSessionId, setFailedBootstrapSessionId] = useState<string | null>(null);
  const provisionPlacement = useMemo(
    () => placeVaultProvisionRequests(messages, vaultRequests),
    [messages, vaultRequests],
  );
  // Provision cards belong beside the Agent reply that owns them. Requests whose
  // owning message is outside the retained window stay unanchored here; they are
  // intentionally not moved into the transcript footer, so opening a Session
  // never turns a historical form into a bottom-fixed card or a scroll target.
  // Mirror the latest messages into a ref (updated every render) so effects that
  // must NOT re-run on every message change — chiefly the deep-link jump effect,
  // whose around-fetch would otherwise be cancelled by an SSE/reconcile update —
  // can still read the current transcript without listing ``messages`` as a dep.
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const deepLinkWindowHandledRef = useRef(false);
  const markVaultRequestHidden = useCallback((_requestId: string) => {
    // The merged provision flow keeps inline provision cards beside their
    // owning replies, so hiding a request no longer needs extra local anchor
    // bookkeeping. Keep the provider hook for compatibility and future
    // extension, but make the callback a stable no-op here.
  }, []);
  const denyVaultProvisionRequest = useCallback(
    async (requestId: string) => {
      try {
        const result = await api.denyVaultRequest(requestId);
        if (!result?.ok) return false;
        showToast(t('vaults.requests.denied'), 'warning');
        return true;
      } catch {
        showToast(t('vaults.approval.errors.failed'), 'warning');
        return false;
      }
    },
    [api, showToast, t],
  );
  const [olderCursor, setOlderCursor] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const loadingOlderRef = useRef(false);
  // True while the loaded window does NOT reach the live tail: entered by a
  // deep-link/search jump into a middle window, AND by the retained-window cap
  // detaching the tail while the reader is scrolled up (see MAX_RETAINED_MESSAGES).
  // Suppresses live append/reconcile and shows the jump-to-latest button, which
  // reloads the tail. Consumers: the SSE-append skip, reconcile skip, the
  // send-path reloadLatest, and the inbox mark-read gate.
  const [historicalWindow, setHistoricalWindow] = useState(false);
  const historicalWindowRef = useRef(false);
  historicalWindowRef.current = historicalWindow;
  const oldestLoadedIdRef = useRef<string | null>(null);
  const newestLoadedIdRef = useRef<string | null>(null);
  // Owned here but driven by the Transcript scroller (which reads/writes it in
  // handleScroll / scrollToBottom): true while the viewport is following the live
  // tail. The retained-window trim reads it to decide which end is safe to drop —
  // dropping the oldest rows is invisible only when the reader is pinned to the
  // bottom, far below them.
  const followingTailRef = useRef(true);
  // Set inside the pinned oldest-trim updater so the [messages] effect can
  // re-point the older cursor against the COMMITTED transcript (robust to a racing
  // append, and idempotent under a double-invoked updater). The newest-side trim
  // detaches the tail synchronously at its ingest point, so it needs no deferred
  // signal here.
  const trimmedOldestRef = useRef(false);
  // Deep-link jump (see deepLinkMessageId): the message id the transcript should
  // scroll to once its window is in the DOM, the id to highlight (~3s fade), and
  // the last ``msg`` value already handled so the jump effect runs once per value.
  const [jumpTarget, setJumpTarget] = useState<string | null>(null);
  const [highlightedId, setHighlightedId] = useState<string | null>(null);
  const handledJumpRef = useRef<string | null>(null);
  const highlightTimerRef = useRef<number | null>(null);
  const [messageFontSize, setMessageFontSize] = useState(() => normalizeChatMessageFontSize(undefined));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // ``working`` = a turn is in flight for this session (from our send, or any
  // other origin we observe). Drives the thinking bubble + the Send→Stop swap.
  const [working, setWorking] = useState(false);
  // Mirror ``working`` into a ref so recovery paths (reconcile) can tell the
  // activity resync whether a turn is still in flight without re-running on it.
  const workingRef = useRef(working);
  workingRef.current = working;
  const [runtimeState, setRuntimeState] = useState<SessionRuntimeState>(emptyRuntimeState);
  const [eventStreamConnected, setEventStreamConnected] = useState(false);
  // Global background-work banner toggle (spec req 2), persisted server-side.
  // Tri-state: null = not yet known → suppress the banner so a stored "off"
  // never flashes on first paint; resolves to the stored value (ON when absent),
  // or ON on a fetch error so a transient failure can't hide live work.
  const [bannerEnabled, setBannerEnabled] = useState<boolean | null>(null);
  // Chat Agent Activity panel (config.ui.show_agent_activity). Default off = a
  // strict no-op: the backend never streams assistant/tool_call rows and this UI
  // never renders anything, so the transcript is byte-for-byte today's.
  //
  // SINGLE SOURCE OF TRUTH: the durable ``GET /api/sessions/<id>/activity`` endpoint
  // owns ALL settled groups (done / failed / interrupted, their anchors, step
  // counts, durations). The live SSE buffer drives ONLY the in-flight running card.
  // On every settle signal (terminal message.new, turn.end, reconnect, visibility)
  // we clear the buffer and rebuild groups from storage via ``refreshActivity`` —
  // so a lossy/gappy stream can never corrupt a settled chip. There is deliberately
  // NO client-side group reconstruction from the buffer.
  //  - ``activityGroups``: settled turns, set ONLY from the endpoint.
  //  - ``liveState``: the in-flight running-card buffer, a pure generation state
  //    machine (see ``liveActivityReducer``). ``liveStateRef`` is the synchronous
  //    source of truth; ``liveRows`` / ``liveStartedAt`` mirror it for rendering.
  const [showAgentActivity, setShowAgentActivity] = useState(false);
  const showAgentActivityRef = useRef(false);
  showAgentActivityRef.current = showAgentActivity;
  const confirmedAgentActivityVisibilityRef = useRef(false);
  const agentActivityVisibilityRequestRef = useRef(0);
  const [activityGroups, setActivityGroups] = useState<ActivityGroup[]>([]);
  const liveStateRef = useRef<LiveActivityState>(initialLiveActivity());
  const [liveRows, setLiveRows] = useState<ActivityRow[]>([]);
  const [liveStartedAt, setLiveStartedAt] = useState<number | null>(null);
  const [activityCardExpanded, setActivityCardExpanded] = useState(false);
  // Tool-row visibility (``config.ui.show_tool_calls``, default on). Global +
  // cross-device: the eye pill and the Settings toggle write the same config field.
  const [showToolCalls, setShowToolCalls] = useState(true);
  const toggleToolCalls = useCallback(() => {
    setShowToolCalls((prev) => {
      const next = !prev;
      // Optimistic; persist the minimal ui patch (save_config deep-merges). A failed
      // save leaves the local flag as the user set it for this session.
      void api.mutateConfig([setConfigField(['ui', 'show_tool_calls'], next)]).catch(() => {});
      return next;
    });
  }, [api]);
  const [expandedActivity, setExpandedActivity] = useState<Record<string, boolean>>({});
  const [loadingActivity, setLoadingActivity] = useState<Record<string, boolean>>({});
  // Groups whose lazy detail fetch failed — the chip shows a retry affordance
  // instead of a misleading "no activity" empty state (transient endpoint failure).
  const [activityError, setActivityError] = useState<Record<string, boolean>>({});
  // Coalesce settle-triggered refreshes: a settle burst (terminal + turn.end) runs
  // one in-flight refresh + at most one trailing refresh, never N fetches. A failed
  // settle fetch schedules exactly one bounded retry (the next settle also rebuilds).
  const activityRefreshInFlightRef = useRef(false);
  const activityRefreshPendingRef = useRef(false);
  // The controller-owned state used to interpret an open durable group. This is
  // deliberately tri-state: a route switch starts unknown, so an early activity
  // read cannot turn "not hydrated yet" into a visible interrupted result.
  const activityForegroundRef = useRef<ActivityForeground>('unknown');
  const activityRetryTimerRef = useRef<number | null>(null);
  // Latest ``scheduleActivityRefresh`` (assigned below) so its own async resolution
  // can re-enter for the trailing / retry pass without a definition cycle.
  const scheduleActivityRefreshRef = useRef<(isRetry?: boolean) => void>(() => {});
  // Advance the live-buffer state machine + mirror it into render state. The ref is
  // updated synchronously so same-tick reads (generation, settled) are current.
  const dispatchLive = useCallback((event: LiveActivityEvent) => {
    const next = liveActivityReducer(liveStateRef.current, event);
    liveStateRef.current = next;
    setLiveRows(next.rows);
    setLiveStartedAt(next.startedAt);
  }, []);
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
  // every "a turn is starting now" path so clear-on-idle stays race-safe. Also sets
  // ``workingRef`` synchronously so a settle refresh in the same tick reads it.
  const markWorking = useCallback(() => {
    // Authoritative running can arrive after an early idle read on navigation.
    // Resume before hydrating, so the next live row cannot discard that history.
    if (liveStateRef.current.settled) dispatchLive({ type: 'turn_start' });
    turnEpochRef.current += 1;
    workingSetAtRef.current = Date.now();
    activityForegroundRef.current = 'running';
    workingRef.current = true;
    setWorking(true);
  }, [dispatchLive]);

  // ----- Agent Activity: the live buffer feeds ONLY the in-flight running card, as
  // a pure generation state machine (see liveActivityReducer). Settled groups come
  // exclusively from the durable endpoint (refreshActivity). -----
  const ingestActivityRow = useCallback(
    (msg: WorkbenchMessage) => {
      dispatchLive({ type: 'row', row: activityRowFromMessage(msg), now: Date.now() });
    },
    [dispatchLive],
  );
  // Rebuild ALL settled groups from durable storage — the single source of truth —
  // for the generation ``issuedGen`` this refresh was scheduled for. Returns false
  // when the fetch fails so the caller can schedule a bounded retry. The live buffer
  // is only touched when the resolution is still for the CURRENT generation (a newer
  // turn.start bumped it → a late resolution is a structural no-op).
  const refreshActivity = useCallback(
    async (issuedGen: number): Promise<boolean> => {
      const sid = sessionIdRef.current;
      if (!sid || !showAgentActivityRef.current) return true;
      let res: { groups: TurnActivityGroupWire[] };
      try {
        res = await api.getSessionActivity(sid);
      } catch {
        return false; // transient failure → caller retries; a stale card is hidden by the working gate
      }
      if (sid !== sessionIdRef.current || !showAgentActivityRef.current) return true;
      const fetched = (res.groups ?? []).map(groupFromWire);
      // Interpret an open group against the LATEST controller state, not the state
      // from when this request started. A chat switch can hydrate ``running`` while
      // an earlier unknown-state request is in flight; committing the stale boolean
      // is the orange interrupted-chip flash this boundary prevents.
      const foreground = activityForegroundRef.current;
      const { settled: groups, inflight } = activityGroupsForForeground(fetched, foreground);
      // Settled groups are always safe to replace (storage is authoritative);
      // preserve already-loaded rows so a resync doesn't force a re-fetch on expand.
      setActivityGroups((prev) => {
        const prevRows = new Map(prev.filter((g) => g.rows).map((g) => [g.id, g.rows] as const));
        return groups.map((g) => (g.rows || !prevRows.has(g.id) ? g : { ...g, rows: prevRows.get(g.id) }));
      });
      if (inflight) {
        // A live tail does not prove history is loaded. Always reconcile the
        // current running generation with its durable rows; the reducer merges
        // overlap and keeps live events that arrived during this read.
        if (liveStateRef.current.gen === issuedGen && !liveStateRef.current.settled) {
          try {
            const wire = await api.getSessionActivityGroup(sid, inflight.id);
            if (sid !== sessionIdRef.current || !showAgentActivityRef.current) return true;
            if (activityForegroundRef.current !== 'running') return true;
            const rows = groupFromWire(wire).rows ?? [];
            if (rows.length > 0) {
              const startMs = Date.parse(rows[0].created_at);
              dispatchLive({
                type: 'rehydrate_for_gen',
                gen: issuedGen,
                rows,
                startedAt: Number.isFinite(startMs) ? startMs : Date.now(),
              });
            }
          } catch {
            return false; // use the same bounded retry as a failed summary read
          }
        }
      } else if (foreground === 'idle') {
        // Authoritative idle: clear the finished buffer so the card swaps to its
        // chip, but only if still the same generation (newer rows are kept).
        dispatchLive({ type: 'clear_for_gen', gen: issuedGen });
      }
      return true;
    },
    [api, dispatchLive],
  );
  // Coalesce settle-triggered refreshes: one in-flight + at most one trailing for
  // the current generation, never N fetches for a settle burst. Each response is
  // interpreted against the latest controller foreground state at commit time. On
  // a transient failure schedule one bounded retry (the next settle also rebuilds).
  const scheduleActivityRefresh = useCallback(
    (isRetry = false) => {
      if (!showAgentActivityRef.current) return;
      if (activityRefreshInFlightRef.current) {
        activityRefreshPendingRef.current = true;
        return;
      }
      activityRefreshInFlightRef.current = true;
      const issuedGen = liveStateRef.current.gen;
      void refreshActivity(issuedGen).then((ok) => {
        activityRefreshInFlightRef.current = false;
        if (activityRefreshPendingRef.current) {
          activityRefreshPendingRef.current = false;
          scheduleActivityRefreshRef.current(false);
        } else if (ok === false && !isRetry && activityRetryTimerRef.current === null) {
          activityRetryTimerRef.current = window.setTimeout(() => {
            activityRetryTimerRef.current = null;
            scheduleActivityRefreshRef.current(true);
          }, 1500);
        }
      });
    },
    [refreshActivity],
  );
  scheduleActivityRefreshRef.current = scheduleActivityRefresh;
  const applyAgentActivityVisibility = useCallback(
    (enabled: boolean) => {
      showAgentActivityRef.current = enabled;
      setShowAgentActivity(enabled);

      if (enabled) {
        // The shortcut means "show me this execution now", not merely "remember
        // the preference for the next turn". Expand first, then hydrate the open
        // durable group while the config write enables subsequent live events.
        setActivityCardExpanded(true);
        scheduleActivityRefresh();
      } else {
        // Bump the generation so detail reads already in flight cannot repopulate
        // the card after it was globally hidden. The next enable hydrates afresh.
        dispatchLive({ type: 'reset' });
        setActivityGroups([]);
        setActivityCardExpanded(false);
        setExpandedActivity({});
        setLoadingActivity({});
        setActivityError({});
        activityRefreshPendingRef.current = false;
        if (activityRetryTimerRef.current !== null) {
          window.clearTimeout(activityRetryTimerRef.current);
          activityRetryTimerRef.current = null;
        }
      }
    },
    [dispatchLive, scheduleActivityRefresh],
  );
  const setAgentActivityVisibility = useCallback(
    (enabled: boolean) => {
      if (!canEditAgentActivityVisibility) return;
      applyAgentActivityVisibility(enabled);
      const request = ++agentActivityVisibilityRequestRef.current;

      const pendingWrite = api.mutateConfig([
        setConfigField(['ui', 'show_agent_activity'], enabled),
      ]);
      void pendingWrite
        .then(() => {
          confirmedAgentActivityVisibilityRef.current = enabled;
          if (request !== agentActivityVisibilityRequestRef.current) return;
          // Close the small save/streaming gap: rows persisted while the config
          // mutation was in flight are recovered from the durable group.
          if (enabled && showAgentActivityRef.current) scheduleActivityRefresh();
        })
        .catch(() => {
          if (request !== agentActivityVisibilityRequestRef.current) return;
          applyAgentActivityVisibility(confirmedAgentActivityVisibilityRef.current);
        });
    },
    [api, applyAgentActivityVisibility, canEditAgentActivityVisibility, scheduleActivityRefresh],
  );
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
  // Same-route refreshes can overlap (for example, the initial load and an
  // authorization refresh). Only the latest bootstrap may commit state: a
  // superseded success or failure describes an older authorization snapshot.
  const bootstrapRequestGenerationRef = useRef(0);
  // Bootstrap, recovery, and jump-to-latest all return authoritative live-tail
  // snapshots. Order their successful commits through one generation so an
  // older response cannot restore a claimed projection after a newer
  // post-settlement snapshot removed it. A failed newer read does not suppress
  // an older successful fallback; the next successful generation still wins.
  const transcriptReadGenerationRef = useRef(0);
  const committedTranscriptReadGenerationRef = useRef(0);
  const beginTranscriptSnapshotRead = useCallback((requestSessionId: string) => {
    const generation = ++transcriptReadGenerationRef.current;
    return () => {
      if (
        requestSessionId !== sessionIdRef.current
        || generation < committedTranscriptReadGenerationRef.current
      ) {
        return false;
      }
      committedTranscriptReadGenerationRef.current = generation;
      return true;
    };
  }, []);

  const appendMessage = useCallback((msg: WorkbenchMessage) => {
    setMessages((prev) => {
      // Ordered single-row insert (deduped, no full re-sort) so an out-of-order
      // live event can't render a reply ahead of its prompt.
      const next = insertMessageOrdered(prev, msg);
      if (next === prev) return prev; // dup — same reference, React skips
      // Bounded retained window: while following the live tail, drop the oldest
      // overflow (far above the pinned viewport, invisible); the [messages] effect
      // re-points the older cursor at the new oldest (before_id is exclusive) so
      // scroll-up pages them back exactly. The scrolled-up case is handled at the
      // ingest points (onMessageNew / reconcile), which detach the tail instead of
      // growing the DOM — so this path never drops a row the reader can see, and
      // the user's own optimistically-appended sends are always kept.
      if (followingTailRef.current && next.length > MAX_RETAINED_MESSAGES) {
        trimmedOldestRef.current = true;
        return next.slice(next.length - MAX_RETAINED_MESSAGES);
      }
      return next;
    });
  }, []);

  // The header's route and backend lock both come from the durable Session row.
  // Every settled turn refreshes it because turn start may materialize inherited
  // model / effort even on an already-bound legacy session. A missed turn.end is
  // recovered by syncTurnState, which refreshes the row when it clears stale work.
  // Read the global banner toggle once per mount (cached GET). Absent/failed →
  // default ON, so a transient prefs error never hides live background work.
  useEffect(() => {
    let cancelled = false;
    api
      .getWorkbenchPrefs()
      .then((prefs) => {
        if (!cancelled) setBannerEnabled(prefs?.background_work_banner_enabled !== false);
      })
      .catch(() => {
        if (!cancelled) setBannerEnabled(true); // default ON on error
      });
    return () => {
      cancelled = true;
    };
  }, [api]);
  // Authoritative reload of the loaded session row, guarded so a late resolve
  // can't stamp one chat's row onto the chat the user moved to. Best-effort by
  // contract: every caller must already be correct if the request never lands.
  const refreshSessionRow = useCallback(async () => {
    const read = async (allowRetry: boolean): Promise<void> => {
      const id = sessionIdRef.current;
      if (!id) return;
      const isCurrent = await sessionRowRefreshGateRef.current.begin();
      try {
        // cache:false — an earlier refresh (page open / reconnect) may have
        // cached a stale row; reusing it inside the read cache's TTL is exactly
        // what the callers are trying to escape.
        const row = await api.getSession(id, { cache: false });
        installServerSession((prev) =>
          isCurrent() && row.id === sessionIdRef.current ? row : prev,
        );
      } catch {
        // If this was still the newest read, one bounded retry prevents a
        // transient failure from discarding an older route-bearing response
        // without turning a persistent outage into an unbounded loop.
        if (allowRetry && isCurrent() && id === sessionIdRef.current) {
          await read(false);
        }
      }
    };
    await read(true);
  }, [api, installServerSession]);

  // Persistence for the header's optimistic edits.
  const sendSessionPatch = useCallback(
    async ({ changes, gate, group }: SessionPatchWrite, patchedId: string): Promise<boolean> => {
      const finishPatch = gate.beginMutation();
      try {
        await api.updateSession(patchedId, changes as any);
        // Do not install the PATCH response: it is only a mutation snapshot and
        // can be older than another committed write. The authoritative refresh
        // on settle is guarded by session id and runs after every active write.
        //
        // The server now holds these fields, so the rollback target moves PAST
        // them: a burst commits in parts (the Agent pick lands, the effort pick
        // folded in behind it is refused), and reverting to where the burst started
        // would undo a change the server took. Only the fields this request
        // carried — the rest of the target is still the pre-burst row.
        commitSessionRowWrite<WorkbenchSession>(patchedId, changes, group);
        return true;
      } catch (err) {
        if (patchedId !== sessionIdRef.current) return false;
        // The archive itself has already converged through the shared
        // ``onSessionArchived`` subscription (the title editor and route picker are
        // gone by the next render, so this PATCH cannot be re-issued). Only the
        // wording is per-verb: the global ``errors.session_archived`` copy that
        // ``handleApiError`` resolved is Show-Page-worded, which is wrong for a
        // rename or a re-route.
        setError(isSessionArchivedError(err) ? t('chat.archived.editBlocked') : (errorMessage(err) ?? String(err)));
        return false;
      } finally {
        finishPatch();
      }
    },
    [api, t],
  );

  // Within ONE group the clicks made while a request is in flight are transit
  // rather than intent, so they fold into a single follow-up PATCH: an effort
  // clicked behind an Agent switch was composed against that switch, so if the
  // request fails the follow-up goes with it and the re-read below shows what the
  // server actually holds — a half-applied route is worse than a visible
  // rollback. A merged payload that ends up carrying the whole route depended on
  // nothing, and ``sessionWriteStandsAlone`` is what keeps it from being dropped
  // for a failure that says nothing about it. Across groups nothing folds, because
  // they no longer share a writer at all. The newest gate wins: it belongs to the
  // mount that is on screen now and whose reads the reconcile below has to fence.
  const mergeSessionPatch = useCallback(
    (prev: SessionPatchWrite, next: SessionPatchWrite): SessionPatchWrite => ({
      changes: { ...prev.changes, ...next.changes },
      gate: next.gate,
      group: next.group,
    }),
    [],
  );

  // Once per burst, not once per write. On success the read is what makes the
  // optimistic row authoritative again, and it is returned rather than fired and
  // forgotten, so the session counts as saving until it has reconciled. Only for
  // the open chat: a burst for a session the user has navigated away from has
  // nothing on screen to reconcile, and ``refreshSessionRow`` reads whatever IS
  // open.
  const settleSessionPatch = useCallback(
    (patchedId: string, committed: boolean, group: SessionWriteGroup) => {
      // Ends this group's open write — including its overlay, so the re-read below
      // is what those fields show from here on — and hands back what a rejection
      // must restore. Per group: the OTHER group's request stands or falls on its
      // own, so releasing both here would drop an overlay nobody has answered.
      const base = releaseSessionRowWrite<WorkbenchSession>(patchedId, group);
      if (patchedId !== sessionIdRef.current) return;
      if (committed) return refreshSessionRow();
      // A rejected burst lives only in this row, and the re-read is best-effort
      // BY CONTRACT — it swallows its own failure — so it cannot be the rollback:
      // an offline tab would keep showing a title or route the server refused,
      // with the saving indicator already gone. Restore the values the burst
      // replaced instead, and only for the fields it changed: putting the whole
      // pre-burst row back would also undo what arrived meanwhile over SSE (a
      // status flip that made the chat read-only, say), or what the sibling group
      // is still writing — neither of which this burst touched.
      if (base) {
        sessionRowRefreshGateRef.current.invalidate();
        applyLocalSession((prev) => (prev && prev.id === patchedId ? { ...prev, ...base } : prev));
      }
      // Converge anyway, unawaited: the restored values are the ones this tab
      // last saw, and only a read can show a field someone else moved. The row on
      // screen is already correct, so nothing waits for it.
      void refreshSessionRow();
    },
    [refreshSessionRow, applyLocalSession],
  );

  // One writer per field group, not one per row. A writer serializes and
  // coalesces per key and ENDS the burst on failure, so its key must name exactly
  // the fields that share a fate — see ``bySessionWriteGroup``. Sharing one key
  // let a refused rename drop a route pick that had never been sent, and revert
  // it. They share the sender: the request is the same PATCH either way, and the
  // server writes only the columns it was given.
  // Whether a refused request takes the write behind it down with it is decided by
  // the group that owns those fields, from the two payloads' relation — never
  // assumed for the key as a whole (a model click behind a refused model click
  // overwrites the very field that failed).
  const patchStandsAlone = useCallback(
    (pending: SessionPatchWrite, refused: SessionPatchWrite) =>
      sessionWriteStandsAlone(pending.group, pending.changes, refused.changes),
    [],
  );

  const { write: writeRoutePatch, isSaving: isRoutePatchSaving } = useCoalescedWrite<SessionPatchWrite>(
    'session-route',
    sendSessionPatch,
    {
      merge: mergeSessionPatch,
      standsAlone: patchStandsAlone,
      onSettled: useCallback(
        (patchedId: string, committed: boolean) => settleSessionPatch(patchedId, committed, 'route'),
        [settleSessionPatch],
      ),
    },
  );
  const { write: writeMetaPatch, isSaving: isMetaPatchSaving } = useCoalescedWrite<SessionPatchWrite>(
    'session-meta',
    sendSessionPatch,
    {
      merge: mergeSessionPatch,
      standsAlone: patchStandsAlone,
      onSettled: useCallback(
        (patchedId: string, committed: boolean) => settleSessionPatch(patchedId, committed, 'meta'),
        [settleSessionPatch],
      ),
    },
  );
  const sessionPatchWriters = useMemo(
    () => ({ route: writeRoutePatch, meta: writeMetaPatch }) as Record<SessionWriteGroup, typeof writeRoutePatch>,
    [writeRoutePatch, writeMetaPatch],
  );

  // ── Converging on a terminal archive this tab missed ────────────────────────
  //
  // A backgrounded / offline tab can miss the archive SSE for a session that
  // already has a native_session_id, and the recovery row read still needs to
  // run on every reconnect/focus — so a 409 ``session_archived`` from
  // the FIRST write the user attempts is the only point at which this tab learns
  // the truth. Whichever write that is: sending, renaming, re-routing the agent,
  // forking a quote out, or any Show Page mutation. Converging per-verb is what
  // produced the same review finding three rounds running, so the fact is applied
  // ONCE here and the verbs just report their own message.
  //
  // Patch first, reload second: the 409 IS the server's answer, and this is
  // precisely the tab whose connectivity is in doubt, so a ``getSession`` that
  // fails must not leave the chat writable. Patching ``status`` flips ``readOnly``,
  // which disables the composer and withdraws every other mutating control; the
  // authoritative refresh then follows, best-effort, for the rest of the frozen row.
  const convergeSessionArchived = useCallback(
    (archivedSessionId: string) => {
      // A late response for a chat the user already left must not stamp its
      // archive onto the chat now mounted (markSessionArchived guards the row
      // identity too; this also skips the needless refresh).
      if (archivedSessionId !== sessionIdRef.current) return;
      showPageRequestRef.current += 1;
      setShowPageBusy(false);
      writeChatViewMode(archivedSessionId, 'chat');
      sessionRowRefreshGateRef.current.invalidate();
      installServerSession((prev) => markSessionArchived(prev, archivedSessionId));
      void refreshSessionRow();
    },
    [refreshSessionRow, installServerSession],
  );

  // Every write that goes through the shared JSON helpers (updateSession,
  // forkSession, ensureShowPage, Show Page access / availability / icon
  // mutations …) reports its archived 409 through this one API-layer
  // subscription, including the ones issued by components this page owns rather
  // than by the page itself. ``sendMessage`` is the exception by construction: it
  // uses a raw ``apiFetch`` so it can read ``queued``/``already_answered`` off a
  // non-2xx-aware response, so it reports the terminal state back through the
  // ApiContext converger explicitly.
  useEffect(() => api.onSessionArchived(convergeSessionArchived), [api, convergeSessionArchived]);

  useEffect(() => {
    oldestLoadedIdRef.current = messages[0]?.id ?? null;
    newestLoadedIdRef.current = messages[messages.length - 1]?.id ?? null;
    // Finish a pinned oldest-trim against the COMMITTED transcript. The flag is
    // only set when rows were actually dropped while following the tail, so older
    // rows certainly remain on the server: re-point the older cursor at the new
    // oldest (paging invariant olderCursor === messages[0].id; before_id is
    // exclusive). Idempotent under a double-invoked updater. The newest-side trim
    // (scrolled up) instead detaches the tail synchronously at the ingest point,
    // so there is no deferred historical flip to reconcile here.
    if (trimmedOldestRef.current) {
      trimmedOldestRef.current = false;
      setOlderCursor(messages[0]?.id ?? null);
    }
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
  const reconcile = useCallback(async ({ force = false }: { force?: boolean } = {}) => {
    if (!sessionId) return;
    if (historicalWindowRef.current && !force) return;
    // Reader scrolled up in an already-capped window: don't recover tail rows they
    // aren't looking at into the DOM — detach the live tail so jump-to-latest
    // reloads it. Synchronous flip (not via the [messages] effect) so the same
    // commit gates mark-read. Bounds repeated-gap growth: once historical, this
    // early-returns above. Mirrors the onMessageNew ingest policy.
    if (
      !force &&
      !followingTailRef.current &&
      messagesRef.current.length >= MAX_RETAINED_MESSAGES
    ) {
      setHistoricalWindow(true);
      return;
    }
    const claimTranscriptSnapshot = beginTranscriptSnapshotRead(sessionId);
    try {
      // tail: the RECENT window (not the oldest page), so a missed latest row in
      // a long chat is actually recovered (Codex P2).
      const res = await api.listSessionMessages(sessionId, { limit: 50, tail: true, cache: false });
      if (!claimTranscriptSnapshot()) return;
      const fresh = res.messages.filter(isTranscriptMessage);
      setMessages((prev) => {
        // The tail endpoint includes the one active claimed projection, if any,
        // so every successful recovery read is authoritative for projection
        // replacement/removal even when turn.end was missed.
        const reconciled = reconcileWorkbenchClaimedDeliveries(prev, fresh);
        if (historicalWindowRef.current) return reconciled;
        const merged = mergeById(reconciled, fresh);
        // Following the tail: keep the window capped. A gap larger than the tail
        // fetch, recovered here, would otherwise blow past the cap until the next
        // live append. Drop the oldest and re-point the cursor below.
        if (followingTailRef.current && merged.length > MAX_RETAINED_MESSAGES) {
          trimmedOldestRef.current = true;
          return merged.slice(merged.length - MAX_RETAINED_MESSAGES);
        }
        return merged;
      });
      if (historicalWindowRef.current) return;
      if (fresh.length) {
        const tailOldest = fresh[0];
        const previousOldestId = oldestLoadedIdRef.current;
        const previousNewest = messagesRef.current[messagesRef.current.length - 1];
        if (
          !previousOldestId ||
          !previousNewest ||
          isTranscriptWindowDisjoint(previousNewest, tailOldest)
        ) {
          setOlderCursor(res.next_before_id ?? null);
        }
      }
    } catch {
      /* keep the current transcript; the next reconnect retries */
    }
    // Resync activity too: an SSE gap can drop the terminal/turn.end, so rebuild
    // settled groups from storage (a recovered turn gets its chip; a still-running
    // turn keeps/re-hydrates its card). Coalesced + no-op when the toggle is off.
    scheduleActivityRefresh();
  }, [api, sessionId, scheduleActivityRefresh, beginTranscriptSnapshotRead]);

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

  // Returns false only when the fetch itself failed, so the transcript scroller can
  // re-arm and let a later scroll retry; true for success / no-op / stale session.
  const loadOlderMessages = useCallback(async (): Promise<boolean> => {
    if (!sessionId || !olderCursor || loadingOlderRef.current) return true;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    try {
      const res = await api.listSessionMessages(sessionId, { limit: 50, beforeId: olderCursor });
      if (sessionId !== sessionIdRef.current) return true; // switched chats mid-fetch
      const older = res.messages.filter(isTranscriptMessage);
      if (older.length) {
        // Bounded retained window: paging up only fires while scrolled to the top
        // (not pinned), so drop the newest overflow — those rows sit far BELOW the
        // viewport, and the manual scroll-anchor tracks the TOP visible row, so
        // removing them can't shift the reader. Older rows are strictly before the
        // current head (before_id is exclusive), so they never overlap and the
        // merged length is deterministic — detach the live tail SYNCHRONOUSLY when
        // it will exceed the cap (not via the [messages] effect) so the same commit
        // that hides the tail also gates mark-read, keeping an unseen trimmed reply
        // unread. jump-to-latest reloads the tail.
        const willDetachTail = messagesRef.current.length + older.length > MAX_RETAINED_MESSAGES;
        setMessages((prev) => {
          const merged = mergeById(prev, older);
          return merged.length > MAX_RETAINED_MESSAGES ? merged.slice(0, MAX_RETAINED_MESSAGES) : merged;
        });
        if (willDetachTail) setHistoricalWindow(true);
      }
      setOlderCursor(res.next_before_id ?? null);
      return true;
    } catch {
      return false; // keep the transcript; caller re-arms so a later scroll retries
    } finally {
      if (sessionId === sessionIdRef.current) {
        loadingOlderRef.current = false;
        setLoadingOlder(false);
      }
    }
  }, [api, olderCursor, sessionId]);

  const reloadLatestMessages = useCallback(async (): Promise<boolean> => {
    if (!sessionId) return false;
    const claimTranscriptSnapshot = beginTranscriptSnapshotRead(sessionId);
    try {
      const res = await api.listSessionMessages(sessionId, { limit: 50, tail: true, cache: false });
      const tailMessages = res.messages.filter(isTranscriptMessage);
      if (!claimTranscriptSnapshot()) return false;
      if (tailMessages.length === 0) return false;
      setMessages(tailMessages);
      setOlderCursor(res.next_before_id ?? null);
      setHistoricalWindow(false);
      deepLinkWindowHandledRef.current = false;
      // Returning to the live tail from a historical window: activity ingestion was
      // suppressed while scrolled away, so resync groups from storage — a turn that
      // finished in history still gets its chip without a full reload.
      scheduleActivityRefresh();
      return true;
    } catch {
      return false;
    }
  }, [api, sessionId, scheduleActivityRefresh, beginTranscriptSnapshotRead]);

  // Cache every edit synchronously so navigation, a tab close, or a disconnected
  // network cannot lose it. Cloud persistence remains debounced and serialized.
  const onDraftChange = useCallback(
    (text: string) => {
      if (!sessionId) return;
      api.cacheSessionDraft(sessionId, text);
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
  const syncTurnState = useCallback(async (options?: { quiet?: boolean }) => {
    if (!sessionId) return;
    const epochAtRequest = turnEpochRef.current;
    try {
      const res = await api.getTurnState(sessionId, { handleError: !options?.quiet });
      if (sessionId !== sessionIdRef.current) return;
      if (res.foreground === 'unknown') return;
      setRuntimeState(res);
      if (res.foreground === 'running') {
        // markWorking (not setWorking): bump the epoch + timestamp so an OLDER
        // overlapping sync whose idle response lands AFTER this one can't clear
        // the Stop we just confirmed live — its captured epoch is now stale (P2).
        markWorking();
        scheduleActivityRefresh();
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
        const recoveredDroppedTurnEnd = workingRef.current;
        activityForegroundRef.current = 'idle';
        workingRef.current = false;
        setWorking(false);
        // Agent Activity: the idle poll recovering a dropped terminal/turn.end is a
        // FIFTH settle signal (same contract as terminal message.new / turn.end /
        // reconnect / visibility) — mark the generation settled and rebuild from
        // storage so the finished turn gets its chip and the stale buffer clears.
        // The card is already hidden by the working gate; this produces the chip.
        if (showAgentActivityRef.current) {
          dispatchLive({ type: 'settle' });
          scheduleActivityRefresh();
        }
        if (recoveredDroppedTurnEnd) void refreshSessionRow();
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
  }, [api, sessionId, markWorking, dispatchLive, scheduleActivityRefresh, refreshSessionRow]);

  // Keep a ref to the latest syncTurnState so the grace-resync timer can call the
  // current closure without baking it into a dependency cycle.
  useEffect(() => {
    syncTurnStateRef.current = syncTurnState;
  }, [syncTurnState]);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    const requestGeneration = ++bootstrapRequestGenerationRef.current;
    const claimTranscriptSnapshot = beginTranscriptSnapshotRead(sessionId);
    const requestIsCurrent = () => (
      sessionId === sessionIdRef.current
      && requestGeneration === bootstrapRequestGenerationRef.current
    );
    setLoading(true);
    setError(null);
    setFailedBootstrapSessionId((current) => current === sessionId ? null : current);
    try {
      // This component is reused across chat routes. A visibility click from the
      // previous route must settle before the next bootstrap reads global config,
      // otherwise the new chat can reinstall the pre-click value.
      await api.waitForAgentActivityConfigMutations();
      if (!requestIsCurrent()) return;
      const activityVisibilityRequest = agentActivityVisibilityRequestRef.current;
      // Initial chat open needs the same recent tail window, queue, draft,
      // route/config state, and current turn state. Fetch them as one bootstrap
      // payload so remote links don't pay a tunnel round-trip per widget.
      const bootstrapIsCurrent = await sessionRowRefreshGateRef.current.begin();
      const bootstrap = await api.getSessionBootstrap(sessionId);
      // Drop a response if the user switched chats or a newer bootstrap for
      // this route began while it was in flight.
      if (!requestIsCurrent()) return;
      // A write still in flight for this session outranks the row the bootstrap
      // answers with: opening this chat again is not a reason to show the route the
      // user has already clicked past. ``installServerSession`` re-applies it.
      const bootstrapRow = bootstrap.session;
      if (bootstrapIsCurrent()) {
        installServerSession(bootstrapRow);
      } else {
        // A newer turn-end/activity read won the row race. Preserve its row if
        // it landed, but keep this successful bootstrap as the fallback while a
        // cold-page recovery is pending or if both bounded attempts fail.
        installServerSession((current) => sessionRowWithBootstrapFallback(
          current,
          sessionId,
          bootstrapRow,
        ));
        void refreshSessionRow();
      }
      // Capability comes from the bootstrap payload, not the session-row race
      // above — set it unconditionally so a lost row race can't strand the
      // composer disabled for a member who can chat.
      setSessionCanChat(Boolean(bootstrap.capabilities?.can_chat));
      setAgents(bootstrap.agents);
      setDefaultAgentName(bootstrap.default_agent_name);
      setMessageFontSize(normalizeChatMessageFontSize(bootstrap.config?.ui?.chat_message_font_size));
      const bootstrapActivityEnabled = Boolean(bootstrap.config?.ui?.show_agent_activity);
      const activityPreferenceIsCurrent = (
        activityVisibilityRequest === agentActivityVisibilityRequestRef.current
      );
      if (activityPreferenceIsCurrent) {
        setShowAgentActivity(bootstrapActivityEnabled);
        showAgentActivityRef.current = bootstrapActivityEnabled;
        confirmedAgentActivityVisibilityRef.current = bootstrapActivityEnabled;
      }
      const activityEnabled = activityPreferenceIsCurrent
        ? bootstrapActivityEnabled
        : showAgentActivityRef.current;
      // Default on: only an explicit ``false`` hides tool rows.
      setShowToolCalls(bootstrap.config?.ui?.show_tool_calls !== false);
      // Merge (not replace) so a row that arrived over the stream during the
      // load isn't clobbered; the session-change reset keeps prior sessions out.
      // Filtered like every other entry point: the transcript decides what it
      // shows by type, whatever a payload happens to contain. First paint is a
      // visibility decision too — unfiltered, a queued annotation opened the chat
      // as a delivered bubble *and* sat in the queue strip, the exact double
      // render the live path already rejects.
      const transcriptSnapshotIsCurrent = claimTranscriptSnapshot();
      if (transcriptSnapshotIsCurrent) {
        setMessages((prev) => mergeById(prev, bootstrap.messages.filter(isTranscriptMessage)));
        setOlderCursor(bootstrap.next_before_id ?? null);
        setHistoricalWindow(false);
      }
      setHydratedTranscriptSessionId(sessionId);
      setFailedBootstrapSessionId(null);
      activityForegroundRef.current = bootstrap.turn_state.foreground;
      setQueue(bootstrap.queued ?? []);
      setInitialDraft(bootstrap.draft?.text ?? '');
      setRuntimeState(bootstrap.turn_state);
      // Restore Stop for a turn that is still running (e.g. opened in another tab
      // or reloaded mid-turn). markWorking on the live branch so a racing
      // syncTurnState idle response can't clear it; an idle load is authoritative
      // for the fresh page, so clear directly (Codex P2).
      if (bootstrap.turn_state.foreground === 'running') markWorking();
      else if (bootstrap.turn_state.foreground === 'idle') {
        workingRef.current = false;
        setWorking(false);
      }
      // Reconcile only after restoring the live generation above, so a recovered
      // running turn's history request is tagged with the generation it belongs to.
      if (activityEnabled) {
        scheduleActivityRefresh();
      } else {
        setActivityGroups([]);
      }
    } catch (err) {
      // A superseded failure must not stamp an error onto the newer request's
      // loading state, even when both requests belong to the same route.
      if (requestIsCurrent()) {
        setError(errorMessage(err) ?? String(err));
        setFailedBootstrapSessionId(sessionId);
      }
    } finally {
      // Same guard: a stale load finishing must not flip the latest request out
      // of its own loading state into a premature not-found / error view.
      if (requestIsCurrent()) setLoading(false);
    }
  }, [api, sessionId, markWorking, scheduleActivityRefresh, refreshSessionRow, beginTranscriptSnapshotRead, installServerSession]);

  // Clear per-session state the instant the session changes (React Router swaps
  // only :sessionId, reusing this instance), before the new session's
  // load/subscribe — so the previous conversation / queue / draft never leak in
  // and the merge in ``refresh`` only ever unions same-session rows.
  useEffect(() => {
    bootstrapRequestGenerationRef.current += 1;
    // The gate is session-scoped. A PATCH for the previous chat may still be
    // pending after navigation, but it must never hold the new chat's bootstrap
    // or recovery reads hostage.
    sessionRowRefreshGateRef.current = createSessionRowRefreshGate();
    // Clear ``session`` too (not just messages/queue/draft): otherwise the header
    // keeps rendering the previous chat's title + agent picker until the new load
    // finishes, and a rename / agent change would patch() the STALE session.id
    // while the URL is already on the new chat (Codex P2). Nulling it shows the
    // loading state until refresh() resolves the new session.
    applyLocalSession(null);
    setSessionCanChat(false);
    setMessages([]);
    setHydratedTranscriptSessionId(null);
    setFailedBootstrapSessionId(null);
    deepLinkWindowHandledRef.current = false;
    setOlderCursor(null);
    setHistoricalWindow(false);
    oldestLoadedIdRef.current = null;
    newestLoadedIdRef.current = null;
    loadingOlderRef.current = false;
    setLoadingOlder(false);
    // Reset the retained-window state so no stale trim signal or follow flag from
    // the previous chat leaks into the fresh one (the Transcript re-pins on open).
    followingTailRef.current = true;
    trimmedOldestRef.current = false;
    // Drop any pending jump/highlight so it can't fire against the new session.
    setJumpTarget(null);
    setHighlightedId(null);
    handledJumpRef.current = null;
    if (highlightTimerRef.current !== null) {
      window.clearTimeout(highlightTimerRef.current);
      highlightTimerRef.current = null;
    }
    workingRef.current = false;
    setWorking(false);
    setRuntimeState(emptyRuntimeState());
    setQueue([]);
    setInitialDraft(sessionId ? api.getCachedSessionDraft(sessionId) : null);
    // Clear all Agent Activity state so the previous session's groups / live buffer
    // never leak into the new chat (refresh re-reads the toggle + summary).
    setActivityGroups([]);
    activityForegroundRef.current = 'unknown';
    // Keep generations monotonic across navigation, including A -> B -> A:
    // matching the Session id again must not admit A's earlier detail response.
    dispatchLive({ type: 'reset' });
    setActivityCardExpanded(false);
    setExpandedActivity({});
    setLoadingActivity({});
    setActivityError({});
    activityRefreshPendingRef.current = false;
    if (activityRetryTimerRef.current !== null) {
      window.clearTimeout(activityRetryTimerRef.current);
      activityRetryTimerRef.current = null;
    }
    // Drop any pending grace-resync so it can't fire against the new session.
    if (graceResyncRef.current !== null) {
      window.clearTimeout(graceResyncRef.current);
      graceResyncRef.current = null;
    }
  }, [api, sessionId, applyLocalSession, dispatchLive]);

  // Persistent per-session subscription: append every transcript-visible
  // ``message.new`` for THIS session for as long as the page is open. An agent
  // ``result`` ends the working state (the turn produced its reply). Harness
  // turns (scheduled / watch) flow through here too — their prompt + reply both
  // appear without the user having sent anything.
  useEffect(() => {
    if (!sessionId) return;
    // Whatever the stream could not deliver, read back from durable storage:
    // dropped message rows, the queue, whether a turn is still running, and a
    // native bind whose turn.end went missing. Both gap edges want exactly
    // this, so they share it rather than each keeping their own copy.
    const catchUpAfterGap = () => {
      void reconcile({ force: true });
      void refreshQueue();
      void syncTurnState({ quiet: true });
      void refreshSessionRow();
    };
    const disconnect = api.connectWorkbenchEvents({
      // NB: match against sessionIdRef.current (the CURRENT route), NOT the
      // captured ``sessionId`` — there is a window after a chat switch before
      // React runs this subscription's cleanup, during which an event for the
      // PREVIOUS chat would otherwise pass the stale check and append into the
      // new chat (Codex P2).
      onMessageNew: (msg) => {
        if (msg.session_id !== sessionIdRef.current) return;
        // Agent Activity rows (assistant / tool_call) only reach the browser when
        // the toggle streams them (message_mirror). Route them to the running
        // buffer — never the transcript. ``isTranscriptMessage`` already excludes
        // them, so the feature-off path is unchanged. A reverse Show Page mark is
        // no longer an ``assistant`` row — it arrives typed ``annotation`` — so it
        // cannot match here and needs no metadata-keyed exemption to escape the
        // activity buffer.
        if (showAgentActivityRef.current && isActivityMessageType(msg.type)) {
          if (!historicalWindowRef.current) ingestActivityRow(msg);
          return;
        }
        if (!isTranscriptMessage(msg)) return;
        if (historicalWindowRef.current) return;
        // Reader scrolled up in an already-capped window: don't grow the DOM with a
        // live row they aren't looking at — detach the live tail so jump-to-latest
        // reloads it. Flip synchronously (not append-then-trim) so the SAME commit
        // gates mark-read: an unseen tail reply stays unread. The row is in SQLite
        // and returns on reload. Only reachable in 300+ row sessions.
        if (!followingTailRef.current && messagesRef.current.length >= MAX_RETAINED_MESSAGES) {
          setHistoricalWindow(true);
          return;
        }
        appendMessage(msg);
        // Harness live rows can precede read-side provenance enrichment. Pull
        // the enriched REST row so trigger/source chips update without reload.
        if (needsHarnessProvenanceReconcile(msg)) void reconcile();
        // Rebuild durable Activity groups for a phase boundary, terminal reply, or
        // detached completion. Only a terminal reply owns this live generation.
        if (showAgentActivityRef.current && shouldRefreshAgentActivityForMessage(msg)) {
          if (isTerminalAgentMessage(msg)) dispatchLive({ type: 'settle' });
          scheduleActivityRefresh();
        }
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
        if (data.session_id === sessionIdRef.current) {
          setRuntimeState((current) => ({ ...current, in_flight: true, foreground: 'running' }));
          // Agent Activity: a new turn begins → bump the generation (fresh empty
          // buffer). Any stale rows from the previous turn become invisible by
          // construction and can never merge with the new turn's rows.
          if (showAgentActivityRef.current) dispatchLive({ type: 'turn_start' });
          markWorking();
        }
      },
      onTurnEnd: (data) => {
        // The controller confirms the turn settled (terminal result, agent error,
        // or user cancel) — the authoritative end of the working state. There is
        // no turn-duration timeout, so this only fires on a REAL terminal signal.
        if (data.session_id === sessionIdRef.current) {
          activityForegroundRef.current = 'idle';
          setRuntimeState((current) => ({ ...current, in_flight: false, foreground: 'idle' }));
          workingRef.current = false;
          setWorking(false);
          // Agent Activity: the turn settled (result / error / interrupt) → mark the
          // generation settled and rebuild from storage. Authoritative idle makes a
          // trailing open group render as interrupted and clears the finished card's
          // buffer — the interrupt case needs no client-side snapshot or grace timer.
          if (showAgentActivityRef.current) {
            dispatchLive({ type: 'settle' });
            scheduleActivityRefresh();
          }
          // Turn start can materialize an inherited route even on an already-
          // bound legacy session. Reload the authoritative row on every settle
          // so the header picks up both that route and a first native bind.
          void refreshSessionRow();
          void syncTurnState();
          void reconcile({ force: true });
        }
      },
      onQueueUpdated: (data) => {
        // The send-while-busy queue changed (enqueue / flush / per-item delete).
        if (data.session_id === sessionIdRef.current) void refreshQueue();
      },
      onSessionActivity: (data) => {
        if (data.session_id === sessionIdRef.current && data.event === 'archived') {
          // The session you're viewing was archived (here or in another tab) —
          // archive is terminal, so cancel any prepared external launch before
          // leaving the chat.
          showPageRequestRef.current += 1;
          setShowPageBusy(false);
          writeChatViewMode(data.session_id, 'chat');
          goBack();
          return;
        }
        // A rename (from the sidebar or elsewhere) broadcasts the new title;
        // keep this chat's header in sync without a reload. Match the CURRENT
        // route via sessionIdRef like the handlers above.
        if (data.session_id !== sessionIdRef.current || data.event !== 'updated') return;
        if (!Object.prototype.hasOwnProperty.call(data, 'title')) return;
        const nextTitle = data.title ?? null;
        sessionRowRefreshGateRef.current.invalidate();
        installServerSession((prev) => {
          if (!prev || prev.id !== data.session_id || prev.title === nextTitle) return prev;
          return { ...prev, title: nextTitle };
        });
        // The activity event carries only a partial Session projection. Its
        // title is newer than an in-flight full-row read, but it cannot replace
        // that read's native bind or materialized route, so retry after the
        // invalidation and converge on the complete committed row.
        void refreshSessionRow();
      },
      // A socket that was down missed whatever happened while it was. This
      // covers every way a stream can break, including a mobile tab suspended
      // without a clean reconnect: ApiContext recycles a stream that cannot
      // prove it survived, so the recovery arrives here (Codex P2). A stream
      // that did prove it fires nothing — it already delivered them.
      onConnected: catchUpAfterGap,
      onAuthorizationChanged: (data) => {
        const currentSessionId = sessionIdRef.current;
        if (!currentSessionId) return;
        // §3.2: /show admission follows the Instance Viewer role, so it is the
        // instance authorization revision (role ladder), not a per-resource ACL,
        // that can flip this session's page access. show_page no longer appears
        // in resource_kinds, so probe on the revision change instead.
        if (data.instance_authorization_revision != null) {
          void probeShowPageAccess(currentSessionId);
        }
        setSessionCanChat(false);
        void api.getSession(currentSessionId, { cache: false })
          .then((nextSession) => {
            installServerSession(nextSession);
            void refresh();
          })
          .catch(() => goBack());
      },
      onConnectionState: (state) => {
        const connected = state === 'connected';
        setEventStreamConnected(connected);
        if (!connected) void syncTurnState({ quiet: true });
      },
      onError: () => {
        // ApiContext owns the explicit retry loop; keep the page usable while
        // the turn-state fallback below reconciles durable activity data.
      },
    });
    return disconnect;
  }, [api, sessionId, appendMessage, reconcile, refresh, refreshQueue, syncTurnState, refreshSessionRow, markWorking, goBack, ingestActivityRow, scheduleActivityRefresh, dispatchLive, probeShowPageAccess, installServerSession]);

  useEffect(() => {
    setRuntimeState((current) => ({ ...current, pending_input_count: queue.length }));
  }, [queue.length]);

  // Reconcile (don't kill) while foreground or background work is present: there is no turn-duration
  // timeout, so a long agent can run for hours and must keep Stop + the indicator
  // the whole time. Instead of a force-clear timer, poll the controller's
  // authoritative ``GET /turn-state`` on an interval while ``working`` is true AND
  // the page is visible. ``syncTurnState``'s grace-guarded logic clears ``working``
  // only when the backend reports ``in_flight:false`` — so a dropped ``turn.end``
  // is recovered, while a still-running turn keeps Stop. Cleared when ``working``
  // flips false / on unmount; skipped while hidden (visibilitychange already
  // reconciles on resume).
  useEffect(() => {
    const hasBackgroundState =
      runtimeState.background_activities.length > 0 ||
      runtimeState.pending_activity_output_count > 0 ||
      runtimeState.connection === 'reconnecting';
    const needsStreamFallback = !eventStreamConnected;
    if (!working && !hasBackgroundState && !needsStreamFallback) return;
    const interval = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      void syncTurnState({ quiet: needsStreamFallback });
    }, hasBackgroundState || needsStreamFallback ? ACTIVITY_RECONCILE_INTERVAL_MS : WORKING_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [working, runtimeState.background_activities.length, runtimeState.pending_activity_output_count, runtimeState.connection, eventStreamConnected, syncTurnState]);

  const sendMessage = useCallback(
    async (
      text: string,
      attachments?: ComposerAttachment[],
      metadata?: Record<string, unknown>,
      references?: MentionReference[],
    ) => {
      if (!writable) return false;
      // NB: no ``working`` guard — sending WHILE a turn runs is the queue
      // feature; the backend enqueues it (202) instead of refusing.
      const ready = (attachments ?? []).filter((a) => a.status === 'ready');
      if (!sessionId || (!text.trim() && ready.length === 0)) return;
      const refs = references ?? [];
      markWorking();
      setError(null);
      try {
        // Plain (non-streaming) POST: the turn runs fire-and-forget on the
        // controller and its reply arrives over the persistent ``message.new``
        // stream — we don't hold the response open. ``apiFetch`` attaches the
        // CSRF token that ``protect_mutating_ui_requests`` requires under
        // remote-access mode (raw ``fetch`` would 403).
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
        // This POST does not wait for the header's route writes, so a prompt sent
        // in the same breath as a model pick can be admitted on the route the row
        // still holds. The client cannot close that window: routing a turn and
        // sending it are separate requests (``POST /messages`` accepts text,
        // content and metadata only), so gating the send would trade the gap for
        // the very latency this optimistic path removes — a slow PATCH would make
        // Enter feel dead. Atomic admission belongs to the server.
        const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody),
        });
        const body = await response.json().catch(() => null);
        if (isSessionArchivedConflict(response.status, body)) {
          // Archive is terminal. Clear the original session's draft even when
          // this response settles after navigation, and do not return false:
          // false tells Composer to restore a retryable submission.
          api.convergeSessionArchived(sessionId);
          if (sessionId === sessionIdRef.current) {
            setWorking(false);
            setError(t('chat.archived.sendBlocked'));
          }
          return;
        }
        if (!response.ok) {
          // A reserved ordinary message may already have cleared the server
          // draft. Start recovery for its original session before any page
          // staleness decision; the old Composer will then persist its restore
          // even if navigation unmounted it. Quick replies never advance drafts.
          if (!metadata?.quick_reply_for && body?.draft_advanced !== false) {
            void api.recoverSessionDraftAfterRejectedSend(sessionId);
          }
          if (sessionId === sessionIdRef.current) {
            void syncTurnStateRef.current?.();
            setWorking(false);
            // Routes answer either the flat ``{"error": "<sentence>"}`` or the shared
            // CODED shape (``{"error": {code, message}, code, message}``) — the
            // runtime-owned session's ``403 reserved_session`` is the latter, and
            // ``String(body.error)`` renders that object as literal "[object Object]".
            const parsed = body ? selectApiErrorFields(body, `HTTP ${response.status}`) : null;
            setError(
              parsed
                ? parsed.code
                  ? t(`errors.${parsed.code}`, { defaultValue: parsed.fallback })
                  : parsed.fallback
                : body?.detail
                  ? String(body.detail)
                  : `HTTP ${response.status}`,
            );
          }
          return false;
        }
        // Reserving an accepted message advances the server draft to a blank
        // revision. Apply that exact causal revision before handling this chat's
        // UI response, so text typed while the POST was in flight is rebased and
        // synced even if the user has already navigated to another session.
        if (
          response.ok
          && body?.draft_advanced === true
          && body?.draft
          && typeof body.draft === 'object'
        ) {
          void api.reconcileSessionDraftAfterSend(sessionId, body.draft);
        }
        // If the user switched chats while this POST was in flight, the response
        // belongs to the previous session — don't append it / mutate working /
        // error on the chat they moved to (Codex P2). The turn still ran for the
        // original session; its rows live there.
        if (sessionId !== sessionIdRef.current) return;
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
        // A turn started — show the user row. If this send happened from a
        // historical search window, first replace that window with the live tail;
        // the persisted prompt belongs there, not grafted below old context.
        if (body?.id) {
          const message = body as WorkbenchMessage;
          if (historicalWindowRef.current) {
            const caughtUp = await reloadLatestMessages();
            if (sessionId === sessionIdRef.current) {
              if (caughtUp) setJumpTarget(message.id);
              else appendMessage(message);
            }
          } else {
            appendMessage(message);
          }
        }
      } catch (err) {
        if (!metadata?.quick_reply_for) {
          void api.recoverSessionDraftAfterRejectedSend(sessionId);
        }
        if (sessionId === sessionIdRef.current) {
          // The request may have raced a turn owned by another tab or source.
          // Reconcile rather than clearing that turn's Stop state optimistically.
          void syncTurnStateRef.current?.();
          setWorking(false);
          setError(errorMessage(err) ?? String(err));
        }
        // Recovery belongs to the original session, while UI mutation is gated
        // above. Returning false also lets an unmounted old Composer persist its
        // submitted text after navigation.
        return false;
      }
    },
    [
      sessionId,
      api,
      appendMessage,
      refreshQueue,
      markWorking,
      reloadLatestMessages,
      t,
      writable,
    ],
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

  // One action path serves inline view changes and external launches. Every
  // target first ensures the page and sends the same first-build prompt; only
  // the inline target changes this chat's remembered surface.
  const performShowPageAction = useCallback(
    async (sid: string, target: 'inline' | 'prepare'): Promise<boolean> => {
      if (!canOpenShowPage || readOnly) return false;
      const request = ++showPageRequestRef.current;
      setShowPageBusy(true);
      try {
        const res = await api.ensureShowPage(sid);
        // Bail if the user switched chats or explicitly selected Chat while ensure
        // was in flight. The request id closes the same-session ?view=chat race.
        if (sessionIdRef.current !== sid || showPageRequestRef.current !== request) return false;
        if (res?.ok) {
          if (target === 'inline') {
            // Public pages are served under /p/<share_id>/; private under /show/<id>/.
            setShowPageUrl(
              showPageEmbeddedPath(
                res.visibility === 'public' && res.share_id
                  ? `/p/${encodeURIComponent(res.share_id)}/`
                  : `/show/${encodeURIComponent(sid)}/`,
              ),
            );
            setShowPageMode(true);
            writeChatViewMode(sid, 'show-page');
          }
          // First open (or a prior prompt that failed to send) asks the agent to
          // build the visualization. sendMessage returns false on a failed send;
          // track it so the NEXT toggle retries — the page row exists after this,
          // so `existed` alone would never re-prompt a created-but-unprompted page.
          // Never on a read-only (archived) session: the store refuses to CREATE a
          // page there, but a session archived after a failed prompt is still in the
          // retry set, and re-prompting it would only 409. The header no longer
          // offers the toggle at all once the session reads archived, so this is the
          // callback-level backstop for an invocation that raced that render.
          if (!readOnly && (res.existed === false || showPagePromptRetryRef.current.has(sid))) {
            void sendMessage(t('chat.showPage.prompt')).then((sent) => {
              if (sent === false) showPagePromptRetryRef.current.add(sid);
              else showPagePromptRetryRef.current.delete(sid);
            });
          }
          return true;
        }
        return false;
      } catch {
        // apiFetch already surfaced a toast; stay in chat view.
        return false;
      } finally {
        if (sessionIdRef.current === sid && showPageRequestRef.current === request) {
          setShowPageBusy(false);
        }
      }
    },
    [canOpenShowPage, readOnly, api, sendMessage, t],
  );

  const openShowPage = useCallback(
    (sid: string) => performShowPageAction(sid, 'inline'),
    [performShowPageAction],
  );

  const prepareShowPageLaunch = useCallback(
    (sid: string) => performShowPageAction(sid, 'prepare'),
    [performShowPageAction],
  );

  const toggleShowPage = useCallback(async () => {
    const sid = sessionId;
    if (!sid) return;
    if (showPageMode) {
      selectChatView(sid, true);
      return;
    }
    await openShowPage(sid);
  }, [openShowPage, selectChatView, sessionId, showPageMode]);

  // Restore a session's last selected surface after its authoritative row loads.
  // Explicit chat/message deep links win, and archived sessions permanently fall
  // back to Chat because their Show Page is offline.
  useEffect(() => {
    const sid = sessionId;
    if (!sid || session?.id !== sid || showPageRestoreAttemptRef.current === sid) return;
    if (readOnly) {
      showPageRestoreAttemptRef.current = sid;
      writeChatViewMode(sid, 'chat');
      setShowPageViewResolved(true);
      return;
    }
    if (showChatSignal || deepLinkMessageId || readChatViewMode(sid) !== 'show-page') {
      showPageRestoreAttemptRef.current = sid;
      setShowPageViewResolved(true);
      return;
    }
    if (showPageRestoreAccess === 'wait') return;
    showPageRestoreAttemptRef.current = sid;
    if (showPageRestoreAccess === 'deny') {
      writeChatViewMode(sid, 'chat');
      setShowPageViewResolved(true);
      return;
    }
    void openShowPage(sid).finally(() => {
      if (sessionIdRef.current === sid && showPageRestoreAttemptRef.current === sid) {
        setShowPageViewResolved(true);
      }
    });
  }, [deepLinkMessageId, openShowPage, readOnly, session?.id, sessionId, showChatSignal, showPageRestoreAccess]);

  // Keep the author preview on the authenticated route. The guest-facing /p
  // route has separate admission rules and intentionally rejects Limited pages
  // until signed-in guest admission is available.
  const handleShowPagePayload = useCallback((next: ShowPageLinkInfo) => {
    const path = editorPath(next);
    if (path) setShowPageUrl(showPageEmbeddedPath(path));
  }, []);

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
    } catch (err) {
      // The cancel request itself threw (network) — surface it; keep Stop.
      if (sessionId === sessionIdRef.current) setError(errorMessage(err) ?? String(err));
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

  // Pull a queued message back into the composer (to edit / resend) and drop it
  // from the queue. Delete server-side FIRST and only append the text once the
  // row is actually gone — otherwise a failed delete would leave the message
  // both queued (still scheduled to send) and editable in the composer, i.e. a
  // duplicate turn. Append (not replace) so an existing draft isn't clobbered.
  const recallQueued = useCallback(
    async (item: WorkbenchMessage) => {
      const sid = sessionId;
      if (!sid) return;
      try {
        const { removed } = await api.removeQueuedMessage(sid, item.id);
        // Bail if the user switched sessions during the request — otherwise we'd
        // stage the old row's text into a different session's composer.
        if (sessionIdRef.current !== sid) return;
        // ``removed: false`` means the row was already gone (double-click, or a
        // concurrent flush/other tab). Don't stage the text — that would requeue
        // a duplicate — just resync the queue.
        if (!removed) {
          void refreshQueue();
          return;
        }
        setQueue((prev) => prev.filter((m) => m.id !== item.id));
        if (item.text) composerRef.current?.appendText(item.text);
      } catch {
        if (sessionIdRef.current === sid) void refreshQueue();
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
    } catch (err) {
      // Same session guard as the success path: a rejection after a chat switch
      // must not clear the new chat's working / stamp this error on it (Codex P2).
      if (sessionId === sessionIdRef.current) {
        setWorking(false);
        setError(errorMessage(err) ?? String(err));
      }
    }
  }, [api, sessionId, queue, t, refreshQueue, markWorking]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Highlight a message for ~3s then fade it out (the actual fade is the CSS
  // ``msg-highlight`` keyframe on the row; this just owns the on/off window).
  // The timer is tracked in a ref so a second jump (or unmount) clears the
  // previous one instead of leaving a stale highlight or a dangling timeout.
  const startHighlight = useCallback((id: string) => {
    if (highlightTimerRef.current !== null) window.clearTimeout(highlightTimerRef.current);
    setHighlightedId(id);
    highlightTimerRef.current = window.setTimeout(() => {
      highlightTimerRef.current = null;
      setHighlightedId(null);
    }, 3000);
  }, []);

  // Deep-link jump: when ?msg=<id> is present and the target session's data has
  // loaded, scroll to + highlight that message. If it's already in the loaded
  // transcript we jump straight there; otherwise we fetch the centered window
  // and replace the transcript with it as read-only context. The user can page
  // older from that window; returning to live tail is an explicit reload via the
  // jump-to-latest button. Guarded by handledJumpRef so it runs exactly once per
  // ``msg`` value, and gated on the session being present + matching the current
  // route (so a stale load can't jump the new chat). The param is cleared at the
  // end either way so a re-render / visibility gap-recovery never re-fires the
  // jump.
  useEffect(() => {
    const targetMsg = deepLinkMessageId;
    if (!targetMsg || !sessionId) return;
    if (handledJumpRef.current === targetMsg) return;
    // Wait until THIS session's initial data is present (refresh resolved and
    // the loaded session matches the route) — before that the loaded-vs-around
    // decision and the scroll target wouldn't be meaningful.
    if (loading || session?.id !== sessionId) return;
    handledJumpRef.current = targetMsg;
    const requestSessionId = sessionId;

    // If the user is viewing this same session in Show Page mode, the chat
    // surface (transcript) is hidden behind the iframe — a scroll + highlight
    // there would be unseen. Exit Show Page mode so the chat is visible for the
    // jump (the user came here from a search result, so they want the message).
    showPageRestoreAttemptRef.current = sessionId;
    selectChatView(sessionId, true);

    // Clear only ``msg`` (preserve any other query params) so a re-render /
    // visibility gap-recovery can't re-fire the jump. Read the live URL so we
    // don't need the reactive ``searchParams`` in this effect's deps.
    const clearParam = () => {
      const next = new URLSearchParams(window.location.search);
      next.delete('msg');
      setSearchParams(next, { replace: true });
    };

    // Already loaded → jump directly, no fetch. Read the CURRENT transcript via
    // the ref so this effect doesn't depend on ``messages`` (an SSE/reconcile
    // update would otherwise re-run it, and its cleanup would cancel the
    // in-flight around-fetch — dropping the fetched window so ``msg`` never
    // scrolls/clears) (Codex P2).
    if (messagesRef.current.some((m) => m.id === targetMsg)) {
      setJumpTarget(targetMsg);
      startHighlight(targetMsg);
      clearParam();
      return;
    }

    // Not loaded → fetch the centered window and swap the transcript to it.
    let cancelled = false;
    void (async () => {
      try {
        const res = await api.listSessionMessages(requestSessionId, { aroundId: targetMsg, cache: false });
        if (cancelled || requestSessionId !== sessionIdRef.current) return;
        const window = res.messages.filter(isTranscriptMessage);
        if (window.length === 0) {
          // Unknown / deleted / cross-session id — leave the normal tail load
          // intact (don't replace messages or highlight); just drop the param.
          clearParam();
          return;
        }
        // Replace the transcript with the centered window. Keep the older cursor
        // for reading above the match. If there are newer rows beyond this
        // window, treat it as historical context and require the down-arrow to
        // reload the live tail; if not, it already reaches the tail and can keep
        // normal pinned/follow behavior.
        setMessages(window);
        const reachesHistoricalWindow = Boolean(res.next_after_id);
        deepLinkWindowHandledRef.current = reachesHistoricalWindow;
        setOlderCursor(res.next_before_id ?? null);
        setHistoricalWindow(reachesHistoricalWindow);
        setJumpTarget(targetMsg);
        startHighlight(targetMsg);
        clearParam();
      } catch {
        // Fetch failed — keep whatever the normal load produced, drop the param
        // so a re-render doesn't loop, and let the user retry from search.
        if (!cancelled && requestSessionId === sessionIdRef.current) clearParam();
      }
    })();
    return () => {
      // ``cancelled`` trips only on session-change / unmount / a new ``msg``
      // target — NOT on a ``messages`` change (it isn't a dep), so an SSE or
      // reconcile update can't cancel the in-flight around-fetch (Codex P2).
      cancelled = true;
    };
    // Depend on ``session?.id`` (stable), NOT the whole ``session`` object: a
    // title / native-bind / agent_status update landing while the around-fetch
    // is in flight would otherwise re-run this effect — running the cleanup
    // (cancelling the fetch), then exiting via handledJumpRef so the fetched
    // window is dropped and ``?msg`` stays unhandled (Codex P2). The closure
    // still reads ``session`` for the ``!session`` / ``session.id !== sessionId``
    // readiness checks; it only needs to re-run when the id changes.
  }, [api, deepLinkMessageId, loading, selectChatView, session?.id, sessionId, setSearchParams, startHighlight]);

  // Re-arm the jump guard once ``?msg=`` is gone. ``clearParam`` (above) nulls
  // the param after handling, so without this re-selecting the SAME search hit
  // (which re-adds the same ``?msg=``) would be treated as already-handled and
  // the jump effect would exit without scrolling/highlighting. The jump effect
  // sets ``handledJumpRef`` BEFORE clearing the param, so this reset only
  // re-enables the NEXT navigation to that id — it can't double-fire the
  // current jump.
  useEffect(() => {
    if (!deepLinkMessageId) handledJumpRef.current = null;
  }, [deepLinkMessageId]);

  // Clear a pending highlight timer on unmount so it can't fire after teardown.
  useEffect(() => {
    return () => {
      if (highlightTimerRef.current !== null) {
        window.clearTimeout(highlightTimerRef.current);
        highlightTimerRef.current = null;
      }
    };
  }, []);

  // The user is actively viewing this session's live transcript, so an agent reply is seen,
  // not "new". Clear unread whenever it appears — on open, or when a realtime
  // inbox.session.updated lands after a reply — so the Inbox/sidebar never badge
  // the chat you're looking at. Reactive to the unread map, so it's race-free
  // against the cross-process event ordering. Owning this on the mounted route
  // also keeps a canceled blocked navigation from clearing unread state early.
  useEffect(() => {
    // Org members without chat capability are viewers; viewing must not
    // consume the owner's unread state.
    if (!canChat) return;
    if (!canMarkConversationRead({
      pageActive,
      routeSurfaceActive,
      sessionReady: !loading && session?.id === sessionId,
      viewResolved: showPageViewResolved,
      historicalWindow,
      showPageActive,
      foregroundAppWindow: isDesktop && foregroundAppWindowId !== null,
    })) return;
    if (sessionId && (unreadBySession[sessionId] ?? 0) > 0) {
      void markInboxRead(sessionId);
    }
  }, [
    sessionId,
    unreadBySession,
    markInboxRead,
    loading,
    session?.id,
    showPageViewResolved,
    historicalWindow,
    pageActive,
    routeSurfaceActive,
    showPageActive,
    isDesktop,
    foregroundAppWindowId,
    canChat,
  ]);

  // The Workbench canvas creates the session and hands its first message over
  // as router state. Replay it once through the compose path so the agent turn
  // starts. Clear the state afterwards so a manual page refresh (which preserves
  // history state) doesn't resend it.
  useEffect(() => {
    const handoff = pendingInitialMessageHandoff({
      handledSessionId: initialHandledSessionRef.current,
      loadedSessionId: session?.id,
      loading,
      locationState: location.state,
      routeSurfaceActive,
      sessionId,
    });
    if (!handoff) return;
    initialHandledSessionRef.current = handoff.sessionId;
    navigate(location.pathname, { replace: true, state: null });
    void sendMessage(handoff.message);
  }, [
    location.state,
    location.pathname,
    loading,
    session?.id,
    sessionId,
    navigate,
    routeSurfaceActive,
    sendMessage,
  ]);

  // Scoped to the open chat: a write still draining for a session the user has
  // left must not spin the header of the one they are looking at. Either group
  // counts — the header shows ONE saving indicator, and the user is owed it for
  // whichever field they just edited.
  const openSessionId = session?.id ?? '';
  const patchSaving = isRoutePatchSaving(openSessionId) || isMetaPatchSaving(openSessionId);

  // A route or title edit lands on the local row within the click and is
  // persisted behind it. The picker highlight and the title are CONTROLLED by
  // ``session``, so anything this waits for is lag the user reads as a frozen UI
  // (the reason the picker no longer greys itself out either).
  const patch = useCallback(
    (changes: Partial<WorkbenchSession>) => {
      if (!session) return;
      const patchedId = session.id;
      // ``invalidate`` stops a row read that is ALREADY in flight from
      // re-installing the pre-patch row on top of the optimistic one.
      const gate = sessionRowRefreshGateRef.current;
      gate.invalidate();
      applyLocalSession((prev) => (prev && prev.id === patchedId ? { ...prev, ...changes } : prev));
      // The row moves as one; the REQUESTS do not. Fields that overwrite each
      // other belong in one serialized, coalescing, fails-together burst — fields
      // that don't must not be tied to one, so the edit is split into the
      // independent writes it actually is (today: at most a route and a title).
      for (const [group, groupChanges] of bySessionWriteGroup(changes)) {
        // Both the id and the gate travel with the write: a patch waiting to flush
        // belongs to the chat that was open when it was clicked, and the gate is
        // per-session (replaced on navigation), so it must never fence the new
        // chat's reads.
        const opened = sessionPatchWriters[group](patchedId, { changes: groupChanges, gate, group });
        // Record the write against the row it is replacing: what a rejection restores,
        // and what every row arriving from the server has to yield to until this write
        // is answered. ``write`` reports which call OPENED the burst, which is the one
        // that starts the record over.
        recordSessionRowWrite(session, groupChanges, opened, group);
      }
    },
    [session, applyLocalSession, sessionPatchWriters],
  );

  // Session-level actions share the sidebar/mobile row model. A read-only or
  // unauthorized session yields no actions and an inert requestArchive, so the
  // header withdraws the menu rather than offering guaranteed failures.
  const titleFieldRef = useRef<TitleFieldHandle | null>(null);
  const {
    actions: sessionActions,
    archiveDialog: sessionArchiveDialog,
    requestArchive,
    canArchive,
  } = useSessionActions({
    session,
    writable: metadataWritable,
    lifecycleWritable: writable,
    // Rename focuses the header's existing click-to-edit title instead of adding
    // a second editor for the same field.
    onRenameStart: () => titleFieldRef.current?.startEditing(),
    onOpenSession: (id) => navigate(`/chat/${encodeURIComponent(id)}`),
    onArchived: () => navigate('/inbox'),
    // The provider cache feeds the sidebar, not this page's own session copy. The
    // write resolves after an await, so only patch if we're still on that session.
    onSessionPatched: (changes, sessionId) => {
      if (sessionId !== sessionIdRef.current) return;
      sessionRowRefreshGateRef.current.invalidate();
      installServerSession((prev) => (prev && prev.id === sessionId ? { ...prev, ...changes } : prev));
      // Pin updates only carry the changed sidebar field. A successful PATCH can
      // race a turn-end row read, so re-read the complete durable projection to
      // keep any newly materialized route in the header.
      void refreshSessionRow();
    },
    archiveHint: ARCHIVE_SHORTCUT_LABEL,
  });

  // ⌘⇧D / Ctrl+Shift+D archives the session being read. It OPENS THE CONFIRM
  // DIALOG — a destructive action never fires straight off a keystroke — and like
  // the shell's ⌘K it wins from inside the composer, because it's a command, not
  // text.
  //
  // Bound only while there IS something to archive: preventDefault on a read-only
  // or still-loading chat would swallow the browser's own ⌘⇧D (bookmark all tabs)
  // and do nothing in return. And "ChatPage is mounted" is not "chat owns the
  // keyboard" — it stays mounted under app windows and dialogs — so a keystroke
  // belonging to a foreground surface is left to that surface (Codex).
  useRouteSurfaceWindowEvent('keydown', (event) => {
    if (!isArchiveSessionKeydown(event, event.target as Element | null)) return;
    event.preventDefault();
    requestArchive();
  }, canArchive);

  // A keydown inside the Show Page iframe never reaches this window, so the same
  // chord is bound to the frame's own document while it is mounted — otherwise the
  // shortcut silently dies as soon as the user clicks into the page they asked for.
  useEffect(() => {
    if (!canArchive || !routeSurfaceActive) return;
    const frame = showPageFrameRef.current;
    if (!frame) return;
    return bindFrameChord(frame, (event) => isArchiveSessionChord(event), requestArchive);
  }, [canArchive, requestArchive, routeSurfaceActive, showPageActive, showPageUrl]);

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

  // Agent Activity chips are positioned relative to a transcript message: 'before'
  // it (done/failed, hugging the reply) or 'after' it (interrupted, below the
  // trigger). Both keyed by anchor id → array (a message can have both, e.g. a done
  // chip before it and an agent-initiated interrupted chip after it). A null anchor
  // (degenerate no-prior-message case) renders at the TOP — never the tail, which
  // belongs exclusively to the live running card.
  const activityByAnchor = useMemo(() => {
    const before = new Map<string, ActivityGroup[]>();
    const after = new Map<string, ActivityGroup[]>();
    const top: ActivityGroup[] = [];
    for (const group of activityGroups) {
      if (!group.anchorMessageId) {
        top.push(group);
        continue;
      }
      const map = group.anchorPosition === 'before' ? before : after;
      const list = map.get(group.anchorMessageId);
      if (list) list.push(group);
      else map.set(group.anchorMessageId, [group]);
    }
    return { before, after, top };
  }, [activityGroups]);
  // Lazy-load a group's rows from the endpoint (history chips arrive as summary
  // only). On failure, record an error so the chip offers retry instead of showing
  // a misleading "no activity" empty state.
  const loadActivityDetail = useCallback(
    (group: ActivityGroup) => {
      if (!sessionId || loadingActivity[group.id]) return;
      const sid = sessionId;
      setLoadingActivity((prev) => ({ ...prev, [group.id]: true }));
      setActivityError((prev) => (prev[group.id] ? { ...prev, [group.id]: false } : prev));
      api
        .getSessionActivityGroup(sid, group.id)
        .then((wire) => {
          if (sid !== sessionIdRef.current) return;
          const full = groupFromWire(wire);
          setActivityGroups((prev) =>
            prev.map((g) => (g.id === group.id ? { ...g, rows: full.rows ?? [] } : g)),
          );
        })
        .catch(() => {
          if (sid === sessionIdRef.current) setActivityError((prev) => ({ ...prev, [group.id]: true }));
        })
        .finally(() => {
          setLoadingActivity((prev) => ({ ...prev, [group.id]: false }));
        });
    },
    [api, sessionId, loadingActivity],
  );
  // Toggle a chip open/closed; lazy-load its rows on first expand (live-completed
  // groups already carry their rows, so no fetch there).
  const toggleActivityGroup = useCallback(
    (group: ActivityGroup) => {
      const willExpand = !expandedActivity[group.id];
      setExpandedActivity((prev) => ({ ...prev, [group.id]: willExpand }));
      if (willExpand && !group.rows) loadActivityDetail(group);
    },
    [expandedActivity, loadActivityDetail],
  );

  if (!sessionId) {
    return <ChatMissing onBack={goBack} />;
  }

  // A direct session→session switch reuses this ChatPage instance. Until the
  // current row arrives, the state can still hold the previous row or be empty
  // while a newer recovery read supersedes bootstrap. Neither means not-found.
  const viewState = chatSessionViewState({
    routeSessionId: sessionId,
    loadedSessionId: session?.id ?? null,
    hydratedTranscriptSessionId,
    failedBootstrapSessionId,
  });
  if (viewState === 'loading') {
    return (
      <div className="flex h-[60vh] flex-col items-center justify-center gap-2 text-muted">
        <Loader2 className="size-5 animate-spin" />
        <span className="text-[12px]">{t('common.loading')}</span>
      </div>
    );
  }

  if (viewState === 'failed' || !session) {
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 py-8">
        <button
          type="button"
          onClick={goBack}
          className="inline-flex items-center gap-1.5 text-[12px] text-cyan-ink hover:underline"
        >
          <ArrowLeft className="size-3.5" />
          {t('chat.back')}
        </button>
        <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
          {error ?? t('chat.notFound')}
        </div>
      </div>
    );
  }

  const agentDisplayName = sessionAgentDisplayName(session, agents);

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
      <VaultProvisionDialogProvider
        key={sessionId ?? 'no-session'}
        requests={vaultRequests}
        onResolved={refreshVaultRequests}
        onProvisionRequestHidden={markVaultRequestHidden}
        onProvisionRequestDenied={denyVaultProvisionRequest}
        disabled={!writable}
      >
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
        onKeyDown={annotation.handleShortcutKeyDown}
        onPointerDownCapture={focusCanvas}
        {...fileDropHandlers}
      >
        {/* Drag-and-drop overlay: shown while files hover anywhere over the chat
            surface. ``pointer-events-none`` lets the drag events bubble to this
            container, whose drop handler stages them on the composer. */}
        {fileDragging && (
          <div className="pointer-events-none absolute inset-0 z-10 m-2 flex items-center justify-center rounded-2xl border-2 border-dashed border-mint/60 bg-background/85 backdrop-blur-sm md:m-3">
            <div className="flex flex-col items-center gap-2 text-mint-ink">
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
          patchSaving={patchSaving}
          onBack={goBack}
          working={working}
          showPageMode={showPageActive}
          showPageBusy={showPageBusy}
          onToggleShowPage={toggleShowPage}
          onPrepareShowPageLaunch={prepareShowPageLaunch}
          onShowPageVisibilityChange={handleShowPagePayload}
          onShareOpenChange={setShareOpen}
          annotation={annotation}
          onAnnotateOpenChange={setAnnotateOpen}
          readOnlyReason={readOnlyReason}
          writable={writable}
          showPageAccess={showPageAccess}
          canOpenShowPage={canOpenShowPage}
          canManageShowPage={canManageShowPage}
          canManageInstance={capabilities.can_manage_instance}
          sessionActions={sessionActions}
          titleFieldRef={titleFieldRef}
        />

      {showPageActive && showPageUrl && (
        // The session's authenticated /show/<id>/ author surface fills the chat area while the
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
          ref={setShowPageIframe}
          onLoad={annotation.handleIframeLoad}
          title={t('chat.showPage.title')}
          src={showPageUrl}
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"
          allow="clipboard-write"
          className={clsx(
            'min-h-0 w-full flex-1 border-0 bg-background',
            (shareOpen || annotateOpen) && 'pointer-events-none',
          )}
        />
      )}

      {/* Chat surface stays MOUNTED while the Show Page is shown — just hidden —
          so unsent composer text + staged attachments survive the toggle instead
          of being discarded on unmount. */}
      <div className={clsx('flex min-h-0 flex-1 flex-col', showPageActive && 'hidden')}>
        {error && (
          <div className="mx-auto mt-3 w-full max-w-[1080px] rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
            {error}
          </div>
        )}
        <Transcript
          messages={messages}
          session={session}
          agentDisplayName={agentDisplayName}
          working={working}
          hasOlder={!!olderCursor}
          loadingOlder={loadingOlder}
          onLoadOlder={loadOlderMessages}
          needsLatestReload={historicalWindow}
          onReloadLatest={reloadLatestMessages}
          jumpTarget={jumpTarget}
          onJumpHandled={() => setJumpTarget(null)}
          highlightedId={highlightedId}
          messageFontSize={messageFontSize}
          onQuickReply={handleQuickReply}
          provisionRequestsByMessage={provisionPlacement.byMessageId}
          onVaultRequestResolved={refreshVaultRequests}
          onQuoteSelection={quoteSelectionToComposer}
          onAskInNewSession={askInNewSession}
          readOnly={!writable}
          followingTailRef={followingTailRef}
          activity={{
            enabled: showAgentActivity,
            beforeAnchor: activityByAnchor.before,
            afterAnchor: activityByAnchor.after,
            topGroups: activityByAnchor.top,
            liveRows,
            liveStartedAt,
            cardExpanded: activityCardExpanded,
            onToggleCard: () => setActivityCardExpanded((v) => !v),
            onEnable: canEditAgentActivityVisibility
              ? () => setAgentActivityVisibility(true)
              : undefined,
            onDisable: canEditAgentActivityVisibility
              ? () => setAgentActivityVisibility(false)
              : undefined,
            expanded: expandedActivity,
            loading: loadingActivity,
            error: activityError,
            onToggleGroup: toggleActivityGroup,
            onRetryGroup: loadActivityDetail,
            showToolCalls,
            onToggleTools: toggleToolCalls,
          }}
          footer={
            // Archive expires every pending vault request for the session in the
            // same transaction that flips the status, so these cards are already
            // empty for a freshly-loaded archived chat. A tab that loaded them
            // BEFORE the archive still holds them in state, though — the same
            // stale-tab case that reaches the archived 409 — and their
            // approve/deny buttons would write to a session that can't accept it.
            sessionId && !readOnly && capabilities.can_use_vault_secrets ? (
              <VaultChatRequests
                requests={pendingApprovals}
                onResolved={refreshVaultRequests}
                onOffscreenApprovalsChange={setOffscreenApprovals}
              />
            ) : null
          }
        />
        <ActivityStrip
          state={runtimeState}
          sessionId={sessionId ?? ''}
          enabled={bannerEnabled === true}
        />
        {/* Archive reclaims all unsent input (queued rows, pending rows, draft),
            so an archived chat loads with an empty queue. A stale tab can still be
            holding pre-archive rows, and every button here writes: Send now POSTs
            the flush, Recall appends into the disabled composer. */}
        {writable && (
          <QueueStrip queue={queue} onRemove={removeQueued} onRecall={recallQueued} onSendNow={sendQueueNow} />
        )}
        {sessionId && !readOnly && capabilities.can_use_vault_secrets && pendingApprovals.length > 0 ? (
          <VaultApprovalFloat offscreen={offscreenApprovals} pending={pendingApprovals} onResolved={refreshVaultRequests} />
        ) : null}
        {/* key by session so the composer remounts per session — its draft-seeding
            + local value reset, instead of carrying across sessions (Codex P2). */}
        {(writable || readOnlyReason !== null) && <Compose
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
          readOnlyReason={readOnlyReason}
        />}
      </div>
      {/* Archive confirm — mounted at the chat surface (not inside the header's
          popover) so the ⌘⇧D chord can open it in Show Page mode too. */}
      {sessionArchiveDialog}
      </div>
      </VaultProvisionDialogProvider>
      </FileViewerProvider>
    </ImageViewerProvider>
  );
};

// Pending send-while-busy messages, shown between the transcript and the
// composer. Queued work is visually grouped, but each Delivery stays
// independently removable and compatible rows merge only at claim time.
// One queued message. Its text is a single truncated line by default; clicking
// it expands to the full wrapped text (and clicking again collapses it) so a
// long queued prompt can be read without sending it.
export const QueueRow: React.FC<{
  item: WorkbenchMessage;
  onRemove: (id: string) => void;
  onRecall: (item: WorkbenchMessage) => void;
}> = ({ item, onRemove, onRecall }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  // A plain selectable element (not a <button>) so desktop users can drag-select
  // the text; a click still toggles expand/collapse — unless the click ended a
  // text selection, in which case we leave the selection alone.
  const toggle = () => {
    if (window.getSelection()?.toString()) return;
    setExpanded((v) => !v);
  };
  // Offer recall only for the user's own text-only queued prompts:
  //  - harness/scheduled rows (source !== 'user') carry provenance flush_queue
  //    needs (suppress-delivery, native-id dedupe) that a plain recall would drop;
  //  - recall can't carry uploaded files (content.attachments), so an attachment
  //    row would silently lose them. Both can still be deleted or left to send.
  const att = (item.content as Record<string, unknown> | undefined)?.attachments;
  const hasAttachments = Array.isArray(att) && att.length > 0;
  const canRecall = item.source === 'user' && !hasAttachments;
  // Rule 08: a queued annotation belongs to the strip and nowhere else, so the
  // strip is where it has to be identifiable. Same title as the card it will
  // become, so the row the user is looking at and the bubble that replaces it
  // read as one thing rather than two.
  //
  // Read straight from the content rather than through ``chatRowKind``: this
  // row's type is ``queued`` by definition — that is precisely why it is here
  // and not in the transcript — so the transcript's mapper would (correctly)
  // classify it as anything but an annotation. The display record is on the row
  // from the moment it is queued; only its type changes when the flush lands.
  const annotationView = readAnnotationView(item.content);
  // ``item.text`` is the annotator's authored words and nothing else, by
  // contract — so an annotation that is only a highlight or only a boxed region
  // has none, and would sit here as a title, a separator, and empty space.
  const standIn = annotationView && annotationStandIn(annotationView, item.text, hasAttachments);
  return (
    <div
      data-queue-row="true"
      className="relative flex items-start gap-2 px-2.5 py-1.5 transition-[background-color,box-shadow,border-radius] hover:z-10 hover:rounded-lg hover:bg-surface-1 hover:ring-1 hover:ring-border focus-within:z-10 focus-within:rounded-lg focus-within:bg-surface-1 focus-within:ring-1 focus-within:ring-border motion-reduce:transition-none"
    >
      <div
        role="button"
        tabIndex={0}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
        aria-expanded={expanded}
        className={clsx(
          'min-w-0 flex-1 cursor-pointer select-text text-left text-[12px] text-foreground',
          expanded ? 'whitespace-pre-wrap break-words' : 'truncate',
        )}
      >
        {annotationView && (
          <>
            <span className="mr-2 inline-flex items-center gap-[5px] align-middle text-[10.5px] font-medium text-cyan-ink">
              <MessageSquareQuote className="size-[11px] shrink-0" />
              {t(annotationTitleKey(annotationView.direction))}
            </span>
            {(item.text || standIn) && <span className="mr-2 text-[11px] text-muted">·</span>}
          </>
        )}
        {item.text}
        {standIn?.kind === 'quote' && (
          // The card's own quote treatment (pin + muted), flattened to the one
          // line the strip has room for.
          <span className="inline-flex items-center gap-[5px] align-middle text-muted">
            <MapPin className="size-[11px] shrink-0" />
            {standIn.quote}
          </span>
        )}
        {standIn?.kind === 'screenshot' && (
          <span className="inline-flex items-center gap-[5px] align-middle text-muted">
            <ImageIcon className="size-[11px] shrink-0" />
            {t('chat.annotation.screenshot')}
          </span>
        )}
      </div>
      {canRecall && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => onRecall(item)}
          aria-label={t('chat.queue.recall')}
          title={t('chat.queue.recall')}
          className="size-6 shrink-0 text-muted hover:text-foreground"
        >
          <Undo2 className="size-3.5" />
        </Button>
      )}
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={() => onRemove(item.id)}
        aria-label={t('chat.queue.remove')}
        title={t('chat.queue.remove')}
        className="size-6 shrink-0 text-muted hover:text-destructive-ink"
      >
        <X className="size-3.5" />
      </Button>
    </div>
  );
};

// Kind glyph + color tint per unified banner item. Backend activities and the
// three harness sources each get a distinct icon and soft-tinted box (design:
// mint / cyan / gold / violet, via the established bg-<color>/15 pattern).
const ACTIVITY_ITEM_ICON: Record<SessionActivityItemKind, LucideIcon> = {
  backend_activity: Terminal,
  watch: Eye,
  task: Clock,
  agent_run: Bot,
};

const ACTIVITY_ITEM_TINT: Record<SessionActivityItemKind, string> = {
  backend_activity: 'bg-mint/15 text-mint-ink',
  watch: 'bg-cyan/15 text-cyan-ink',
  task: 'bg-gold/15 text-gold-ink',
  agent_run: 'bg-violet/15 text-violet-ink',
};

// One expanded popover row: colored kind icon box + two-line label / subtitle.
// Backend activities are display-only with a colored status word (no landing
// page); harness rows navigate to their Harness surface on click.
const ActivityRow: React.FC<{
  item: SessionActivityState;
  onNavigate: (item: SessionActivityState) => void;
}> = ({ item, onNavigate }) => {
  const { t } = useTranslation();
  const kind = activityItemKind(item);
  const Icon = ACTIVITY_ITEM_ICON[kind];
  const kindLabel = t(`chat.activities.kind.${activityKindI18nKey(item)}`);
  const label = resolveActivityLabel(item, kindLabel);
  const relative = formatRelativeTime(item.since ?? item.started_at, t);
  const isHarness = kind !== 'backend_activity';
  // A queued delegated run is waiting, not executing — mute its icon box so it
  // cannot read as in-progress (its kind label already says "Queued message").
  const queued = isQueuedRun(item);
  const subtitle =
    kind === 'backend_activity' && item.backend ? `${item.backend} · ${relative}` : relative;
  const body = (
    <>
      <span
        className={clsx(
          'flex size-8 shrink-0 items-center justify-center rounded-lg',
          queued ? 'bg-surface-2 text-muted' : ACTIVITY_ITEM_TINT[kind],
        )}
      >
        <Icon className="size-4" aria-hidden="true" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col text-left">
        <span className="truncate text-[13px] font-medium text-foreground">
          <span className="text-muted">{kindLabel} · </span>
          {label}
        </span>
        <span className="truncate text-[11px] text-muted">{subtitle}</span>
      </span>
      {isHarness ? (
        <ChevronRight className="size-4 shrink-0 self-center text-muted" aria-hidden="true" />
      ) : (
        <span className="shrink-0 self-center text-[11px] font-medium text-mint-ink">
          {t('chat.activities.status.running')}
        </span>
      )}
    </>
  );
  if (!isHarness) {
    return <div className="flex items-center gap-2.5 rounded-lg px-2 py-1.5">{body}</div>;
  }
  return (
    <button
      type="button"
      onClick={() => onNavigate(item)}
      title={t('chat.activities.openHarness', { kind: kindLabel })}
      aria-label={t('chat.activities.openHarness', { kind: kindLabel })}
      className="flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 transition-colors hover:bg-surface-2"
    >
      {body}
    </button>
  );
};

// The unified background-work banner: always ONE pill (count = union size); a
// click expands an UPWARD popover (req 1) of per-kind rows sorted running-first
// (req 5), max-height ~340 with internal scroll (req 3). Hidden entirely when
// the global toggle is off (req 2) or the union is empty (current behavior).
const ActivityStrip: React.FC<{
  state: SessionRuntimeState;
  sessionId: string;
  enabled: boolean;
}> = ({ state, sessionId, enabled }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const active = useMemo(
    () => sortBackgroundActivities(state.background_activities),
    [state.background_activities],
  );
  const pendingOutputs = state.pending_activity_output_count;
  if (!enabled) return null;
  if (active.length === 0 && pendingOutputs === 0) return null;

  const first = active[0];
  const firstLabel = first
    ? resolveActivityLabel(first, t(`chat.activities.kind.${activityKindI18nKey(first)}`))
    : '';
  // Mute the pill only when the banner consists SOLELY of queued delegated
  // runs — a waiting message must not glow like live work. Any running item,
  // watch, or task in the union keeps today's active treatment (pending items
  // tie on rank and order by time, so checking only the headline row would
  // mute a banner that still contains an enabled watch or scheduled task).
  const queuedOnly = active.length > 0 && active.every(isQueuedRun);
  const expandable = active.length > 0;
  const navigateTo = (path: string) => {
    setOpen(false);
    navigate(path);
  };

  const pill = (
    <StatusPill
      tone={queuedOnly ? 'idle' : 'running'}
      role="status"
      aria-live="polite"
      className={clsx(
        'min-h-7 min-w-0 max-w-full gap-2 px-3 py-1 text-[11px] font-normal',
        !queuedOnly && 'shadow-sm shadow-mint/5',
        expandable && 'cursor-pointer select-none',
      )}
      indicator={
        active.length > 0 ? (
          <Activity
            className={clsx('size-3.5 shrink-0', queuedOnly ? 'text-muted' : 'text-mint-ink')}
            aria-hidden="true"
          />
        ) : (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-mint-ink" aria-hidden="true" />
        )
      }
      label={
        <>
          <span className="min-w-0 truncate text-muted" title={firstLabel || undefined}>
            {active.length > 0
              ? t('chat.activities.running', { count: active.length })
              : t('chat.activities.delivering', { count: pendingOutputs })}
            {firstLabel ? ` · ${firstLabel}` : ''}
          </span>
          {expandable ? (
            <ChevronDown
              className={clsx('size-3.5 shrink-0 text-muted transition-transform', open && 'rotate-180')}
              aria-hidden="true"
            />
          ) : null}
        </>
      }
    />
  );

  return (
    <div className="shrink-0 px-4 py-2 md:px-8">
      <div className="mx-auto w-full max-w-[1080px]">
        {expandable ? (
          <Popover open={open} onOpenChange={setOpen}>
            <PopoverTrigger asChild>
              <button
                type="button"
                aria-label={t('chat.activities.toggle')}
                className="block w-fit max-w-[min(420px,100%)] rounded-full outline-none focus-visible:ring-2 focus-visible:ring-mint/40"
              >
                {pill}
              </button>
            </PopoverTrigger>
            <PopoverContent
              side="top"
              align="start"
              sideOffset={8}
              // Spec req 1: anchor to the pill's top edge and never open downward
              // (a downward flip would cover the composer). Disable Radix
              // collision flipping so the popover stays above even when zoomed.
              avoidCollisions={false}
              className="flex max-h-[340px] w-[420px] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-lg border-border-strong p-0 shadow-lg"
            >
              <div className="flex max-h-[300px] flex-col gap-0.5 overflow-y-auto p-1">
                {active.map((item) => (
                  <ActivityRow
                    key={item.id}
                    item={item}
                    onNavigate={(it) => navigateTo(harnessNavPath(it, sessionId))}
                  />
                ))}
              </div>
              <button
                type="button"
                onClick={() => navigateTo('/harness')}
                className="flex shrink-0 items-center justify-center gap-1 border-t border-border/60 px-2 py-2 text-[12px] font-medium text-cyan-ink transition-colors hover:bg-surface-2"
              >
                {t('chat.activities.manageInHarness')}
                <ArrowRight className="size-3.5" aria-hidden="true" />
              </button>
            </PopoverContent>
          </Popover>
        ) : (
          pill
        )}
      </div>
    </div>
  );
};

export const QueueStrip: React.FC<{
  queue: WorkbenchMessage[];
  onRemove: (id: string) => void;
  onRecall: (item: WorkbenchMessage) => void;
  onSendNow: () => void;
}> = ({ queue, onRemove, onRecall, onSendNow }) => {
  const { t } = useTranslation();
  if (queue.length === 0) return null;
  return (
    <div className="shrink-0 px-4 md:px-8">
      <div className="mx-auto w-full max-w-[1080px] rounded-xl border border-cyan/25 bg-cyan/[0.04] p-2">
        <div className="flex items-center justify-between px-1 pb-1.5">
          <span className="inline-flex items-center gap-1.5 font-mono text-[10px] font-bold uppercase tracking-[0.1em] text-cyan-ink">
            <Clock className="size-3" />
            {t('chat.queue.title', { count: queue.length })}
          </span>
          <Button type="button" variant="ghost" size="sm" onClick={onSendNow} className="h-6 px-2 text-[11px] text-cyan-ink">
            {t('chat.queue.sendNow')}
          </Button>
        </div>
        <div
          data-queue-batch="true"
          className="flex max-h-32 flex-col overflow-y-auto rounded-lg bg-surface-2"
        >
          {queue.map((item) => (
            <QueueRow key={item.id} item={item} onRemove={onRemove} onRecall={onRecall} />
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
  // Read-only session: the composer is inert and explains why — which is the reason
  // this is the REASON and not a boolean. "Archived, read-only" is the wrong sentence
  // on a runtime-owned row that was never archived.
  readOnlyReason: SessionReadOnlyReason | null;
}

const Compose: React.FC<ComposeProps> = ({ composerRef, onSend, onStop, busy, sessionId, initialDraft, onDraftChange, onSearchAgents, onSearchSessions, readOnlyReason }) => {
  const { t } = useTranslation();
  const readOnly = readOnlyReason !== null;
  return (
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
        // Read-only archived session: reuse the composer's own disabled +
        // placeholder props rather than swapping in a notice bar. ``busy`` is NOT
        // reliably false here — archive commits before the controller turn is
        // cancelled, and this page bootstraps ``working`` from the controller's
        // turn state — so the composer itself suppresses the busy branch while
        // ``disabled`` (``busyControls``), which is what keeps Stop off an archived
        // chat and lets this placeholder win. autoFocus is dropped so opening an
        // archived chat doesn't pop the keyboard on an inert box.
        disabled={readOnly}
        placeholder={
          readOnlyReason === 'system'
            ? // A runtime-owned row (the workspace-notifications session): the server
              // answers ``403 reserved_session`` here, so say what it receives rather
              // than "archived", which it is not.
              t('chat.compose.placeholderSystem')
            : readOnlyReason === 'archived'
              ? t('chat.compose.placeholderArchived')
              : undefined
        }
        autoFocus={!readOnly}
      />
    </div>
  );
};

interface TitleFieldHandle {
  startEditing: () => void;
}

interface ChatHeaderBarProps {
  session: WorkbenchSession;
  agents: VibeAgentBrief[];
  defaultAgentName: string | null;
  // Applies to the local row first, then persists — never awaited by the header.
  onPatch: (changes: Partial<WorkbenchSession>) => void;
  /** A queued ``onPatch`` write is still in flight. */
  patchSaving?: boolean;
  onBack: () => void;
  working: boolean;
  // The EFFECTIVE mode (ChatPage passes ``showPageActive``): whether the page is
  // actually framed right now, which a read-only session is never.
  showPageMode: boolean;
  showPageBusy: boolean;
  onToggleShowPage: () => void;
  onPrepareShowPageLaunch: (sessionId: string) => Promise<boolean>;
  onShowPageVisibilityChange?: (payload: ShowPageLinkInfo) => void;
  onShareOpenChange?: (open: boolean) => void;
  annotation: AnnotationBridge;
  onAnnotateOpenChange?: (open: boolean) => void;
  // Read-only session: the title and the agent route render as static text — the
  // server refuses both edits (409 archived / 403 reserved) — and the Show Page action
  // cluster is withdrawn entirely (see showPageControlActions). The REASON, not a
  // boolean, because it also picks the badge: a runtime-owned row is not "Archived".
  readOnlyReason: SessionReadOnlyReason | null;
  writable?: boolean;
  showPageAccess?: ShowPageAccess | null;
  canOpenShowPage?: boolean;
  canManageShowPage?: boolean;
  canManageInstance?: boolean;
  // Shared session actions, rendered behind the mobile-only ⋯ at the far right.
  // Empty (or absent) withdraws the trigger — which is what a read-only session
  // yields, since every one of those writes is refused.
  sessionActions?: SessionActionDescriptor[];
  // Lets the menu's Rename row focus the title field that already lives here.
  titleFieldRef?: React.Ref<TitleFieldHandle>;
}

// Exported for the read-only regression test (ChatArchivedReadOnly.test.tsx),
// which renders the header alone rather than mounting the whole page. Note the
// live (non-readOnly) header pulls in AgentRoutePicker → useApi, so only the
// read-only rendering is reachable without an ApiProvider.
export const ChatHeaderBar: React.FC<ChatHeaderBarProps> = ({ session, agents, defaultAgentName, onPatch, patchSaving, onBack, working, showPageMode, showPageBusy, onToggleShowPage, onPrepareShowPageLaunch, onShowPageVisibilityChange, onShareOpenChange, annotation, onAnnotateOpenChange, readOnlyReason, writable = readOnlyReason === null, showPageAccess = null, canOpenShowPage = true, canManageShowPage = true, canManageInstance = false, sessionActions, titleFieldRef }) => {
  const { t } = useTranslation();
  const readOnly = !writable;
  const sessionReadOnly = readOnlyReason !== null;
  const showPageActions = showPageControlActions(sessionReadOnly, showPageMode);
  const showLaunchControl = showPageActions.visualize && (showPageMode || canOpenShowPage);
  // Share lives ONLY while the page is framed (owner ruling 2026-08-17): the
  // chat header never carries it, for anyone — access-only managers reach it
  // from the framed view (or the app window title bar) instead.
  const showShareControl = showPageActions.share && canManageShowPage;
  // ``!readOnly`` twice over: useSessionActions already yields an empty list for a
  // read-only session, and the withdrawal is re-stated here so this header cannot
  // grow a ⋯ full of guaranteed-409 rows if a future caller passes actions anyway.
  const mobileSessionActions = sessionActions ?? [];
  const hasMobileSessionActions = writable && mobileSessionActions.length > 0;
  const defaultAgent = defaultAgentName ? agents.find((agent) => agent.name === defaultAgentName) : null;
  const sessionAgentLabel = sessionAgentDisplayName(session, agents);
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
  const pendingForkBackend =
    !session.native_session_id &&
    session.metadata?.created_via === 'session_fork' &&
    typeof session.metadata?.fork_source_backend === 'string'
      ? session.metadata.fork_source_backend.trim() || null
      : null;
  const backendLocked = Boolean(session.native_session_id) || working || Boolean(pendingForkBackend);
  const pinnedBackend = pendingForkBackend ?? (backendLocked ? concreteBackend : null);
  const canClearToDefault = !backendLocked;
  const pickerDisabled = working && !concreteBackend && !pendingForkBackend;
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
        <TitleField
          key={session.id}
          ref={titleFieldRef}
          title={session.title}
          onCommit={(title) => onPatch({ title })}
          readOnly={readOnly}
        />
        {/* Hidden while the Show Page is open so the view gets the full width.
            On a read-only session the route is frozen, so show it as static text
            plus a badge naming WHY instead of an interactive picker. A runtime-owned
            row has no backend at all (``agent_backend`` is empty by design), so the
            agent name — which would fall back to the default agent's, naming a route
            this session will never run — is omitted there rather than invented. */}
        {!showPageMode && readOnly && (
          <div className="flex min-w-0 shrink-0 items-center gap-1.5">
            {readOnlyReason !== 'system' && (
              <span className="truncate text-[12px] font-medium text-muted">
                {sessionAgentLabel || (defaultAgent ? defaultAgent.name : t('newSession.defaultAgent'))}
              </span>
            )}
            {readOnlyReason && (
              <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-[10px] font-bold">
                {readOnlyReason === 'system' ? t('common.systemSession') : t('common.archived')}
              </Badge>
            )}
          </div>
        )}
        {!showPageMode && !readOnly && (
          <AgentRoutePicker
            value={session}
            agents={agents}
            onChange={onPatch}
            saving={patchSaving}
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
        )}
        {/* Chat hides the brand header, so mount the install nudge here too —
            IM-launched users often land straight in a chat. Renders only on iOS
            Safari + not-installed; null otherwise. */}
        <InstallHint />
        {/* Right-aligned actions. The Show Page toggle swaps the chat surface for
            this session's Show Page (the header bar stays); first open initializes
            the page + prompts the agent. It shows its label on desktop and stays
            icon-only on mobile. In Show Page mode a Share control sits beside the
            back-to-chat button. */}
        {/* In Show Page mode the order is: annotation control, launch/back, Share.
            Share exists only there — never on the plain chat header. */}
        {(showLaunchControl || showShareControl || hasMobileSessionActions) && (
          <div className="ml-auto flex items-center gap-1.5">
            {showPageActions.annotate && writable && (
              <ShowPageAnnotateControl
                state={annotation.state}
                onEnable={annotation.enable}
                onDisable={annotation.disable}
                onSetMode={annotation.setMode}
                onPopoverOpenChange={onAnnotateOpenChange}
              />
            )}
            {showLaunchControl && (
              <ShowPageLaunchControl
                sessionId={session.id}
                title={session.title}
                showPageMode={showPageMode}
                busy={showPageBusy}
                onToggle={onToggleShowPage}
                onPrepareLaunch={onPrepareShowPageLaunch}
              />
            )}
            {showShareControl && (
              <ShowPageShareControl
                key={session.id}
                sessionId={session.id}
                initialAccess={showPageAccess}
                canManageInstance={canManageInstance}
                onPayloadChange={onShowPageVisibilityChange}
                onOpenChange={onShareOpenChange}
              />
            )}
            {/* The chat-level session menu is a compact-mobile affordance. Desktop
                keeps these operations in the sidebar instead of duplicating a
                second menu in the page header. */}
            {hasMobileSessionActions && (
              <MobileChatSessionActionMenu
                actions={mobileSessionActions}
                label={t('workbench.sessionActions')}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
};

interface TitleFieldProps {
  title: string | null;
  onCommit: (next: string | null) => void;
  // Archived session: render the title as plain text, with no edit affordance.
  readOnly?: boolean;
}

const TitleField = forwardRef<TitleFieldHandle, TitleFieldProps>(({ title, onCommit, readOnly }, ref) => {
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

  // The ⋯ menu's Rename row drives the header title the user already sees
  // instead of opening a second dialog for the same field. Inert while
  // read-only, where the title is static text with no input to focus.
  useImperativeHandle(
    ref,
    () => ({
      startEditing: () => {
        if (readOnly) return;
        setEditing(true);
        inputRef.current?.focus();
      },
    }),
    [readOnly],
  );

  if (readOnly) {
    return (
      <span className="min-w-0 flex-1 truncate text-[16px] font-bold text-foreground">
        {title || t('chat.untitled')}
      </span>
    );
  }

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
});
TitleField.displayName = 'TitleField';

interface TranscriptProps {
  messages: WorkbenchMessage[];
  session: WorkbenchSession;
  agentDisplayName: string | null;
  working: boolean;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void | Promise<boolean>;
  needsLatestReload: boolean;
  onReloadLatest: () => Promise<boolean>;
  // Deep-link jump (P5): the message id to scroll to once it's in the DOM, a
  // callback to ack the jump (so it runs once per target), and the id currently
  // highlighted (~3s mint fade on the matching row).
  jumpTarget: string | null;
  onJumpHandled: () => void;
  highlightedId: string | null;
  messageFontSize: number;
  onQuickReply: (messageId: string, choice: string) => boolean | void | Promise<boolean | void>;
  provisionRequestsByMessage: Map<string, VaultRequest[]>;
  onVaultRequestResolved: () => void;
  // Chat-selection toolbar: quote the selection into the composer, or fork +
  // ask in a new session seeded with the quote.
  onQuoteSelection: (text: string) => void;
  onAskInNewSession: (text: string) => void;
  // Archived session: the transcript stays fully readable, but every control
  // that would write to THIS session is withdrawn — an old quick reply would
  // POST a message (409), and Quote would insert into a composer that can never
  // send. Hidden/frozen rather than left clickable-and-erroring.
  readOnly: boolean;
  // Owned by ChatPage, driven here: true while the viewport follows the live
  // tail. Lifted so the retained-window trim (ChatPage.appendMessage) can tell
  // when dropping the oldest rows is safe (reader pinned to the bottom).
  followingTailRef: React.MutableRefObject<boolean>;
  // Agent Activity panel state (undefined-safe: when ``enabled`` is false the
  // transcript renders exactly as before — the ThinkingBubble and nothing else).
  activity?: {
    enabled: boolean;
    beforeAnchor: Map<string, ActivityGroup[]>;
    afterAnchor: Map<string, ActivityGroup[]>;
    topGroups: ActivityGroup[];
    liveRows: ActivityRow[];
    liveStartedAt: number | null;
    cardExpanded: boolean;
    onToggleCard: () => void;
    onEnable?: () => void;
    onDisable?: () => void;
    expanded: Record<string, boolean>;
    loading: Record<string, boolean>;
    error: Record<string, boolean>;
    onToggleGroup: (group: ActivityGroup) => void;
    onRetryGroup: (group: ActivityGroup) => void;
    showToolCalls: boolean;
    onToggleTools: () => void;
  };
  // Rendered at the end of the scroll content for approval cards and the brief
  // pre-reply window where a provision request has no Agent message to own yet.
  footer?: React.ReactNode;
}

// Exported for tests (like ChatHeaderBar / MessageRow / ThinkingBubble below):
// the older-page trigger is a property of this subtree, and driving it through
// the whole ChatPage would prove it only for one wiring of the props.
export const Transcript: React.FC<TranscriptProps> = ({
  messages,
  session,
  agentDisplayName,
  working,
  hasOlder,
  loadingOlder,
  onLoadOlder,
  needsLatestReload,
  onReloadLatest,
  jumpTarget,
  onJumpHandled,
  highlightedId,
  messageFontSize,
  onQuickReply,
  provisionRequestsByMessage,
  onVaultRequestResolved,
  onQuoteSelection,
  onAskInNewSession,
  readOnly,
  followingTailRef,
  activity,
  footer,
}) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { openApp } = useWindowManager();
  const fileViewer = useFileViewer();
  const openLocalFile = useCallback(async (target: LocalFileLinkTarget) => {
    const pathLabel = recentPathLabel(target.path);
    const desktop = isDesktopViewport();
    const openPreview = (name: string, size: number | null, mime: string | null, ext: string | null) => {
      if (desktop) {
        openApp('preview', { title: name, params: { path: target.path, name } });
      } else if (fileViewer) {
        fileViewer.open({ kind: 'local', path: target.path, name, size, mime, ext });
      } else {
        downloadFile(target.path);
      }
    };
    const openEditor = (filename: string, mtime: number | null) => {
      const launch = { ...target, filename, mtime };
      if (desktop) openApp('editor', { title: filename, params: launch });
      else navigate('/apps/editor', { state: launch });
    };

    try {
      const meta = await fileMeta(target.path);
      if (previewOverlayKind(meta)) {
        openPreview(meta.name || pathLabel, meta.size, meta.mime, meta.ext);
      } else if (isEditableMeta(meta)) {
        openEditor(meta.name || pathLabel, meta.mtime);
      } else {
        downloadFile(target.path);
      }
    } catch {
      // Mirror the File Browser's name-only fallback when metadata is unavailable.
      const fallback = { kind: 'file', name: pathLabel, size: null };
      if (previewOverlayKind(fallback)) openPreview(pathLabel, null, null, null);
      else if (isEditableFile(fallback)) openEditor(pathLabel, null);
      else downloadFile(target.path);
    }
  }, [fileViewer, navigate, openApp]);
  const selectionActions = transcriptSelectionActions(session, readOnly);
  const forkSourceSessionId =
    typeof session.metadata?.fork_source_session_id === 'string'
      ? session.metadata.fork_source_session_id
      : null;
  const forkSourceSessionTitle =
    typeof session.metadata?.fork_source_session_title === 'string' &&
    session.metadata.fork_source_session_title.trim()
      ? session.metadata.fork_source_session_title.trim()
      : null;
  const isForkedSession = session.metadata?.created_via === 'session_fork';
  const forkSourceBanner =
    isForkedSession && forkSourceSessionId ? (
      <ForkSourceBanner sourceSessionId={forkSourceSessionId} sourceTitle={forkSourceSessionTitle} />
    ) : null;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  // Set just before a programmatic deep-link jump scroll and cleared once it
  // settles. While set, the manual scroll-anchor (captureAnchor + the
  // ResizeObserver restore) early-returns so it can't fight the jump — the jump
  // moves scrollTop to center the target, and the anchor logic would otherwise
  // immediately yank it back to the row it had remembered. A ref (not state) so
  // the scroll handler + observer read it synchronously with no re-render.
  const suppressAnchorRef = useRef(false);
  // ``true`` while the viewport is FOLLOWING the bottom (at/near it) — drives the
  // auto-follow of new content and hides the jump button. A ref so the scroll
  // handler + ResizeObserver read it mid-layout without stale closures, and owned
  // by ChatPage (see followingTailRef) so its retained-window trim reads the same
  // follow state. It is the shadow of ``followingTail`` below and is written ONLY
  // by that state's setter — see the note there for why.
  const pinnedRef = followingTailRef;
  // While the user has scrolled UP to read history (not pinned), remember the
  // topmost row still in view and how far its top sits below the viewport top, so
  // any later content resize can put that exact row back where it was. This is a
  // manual scroll-anchor: iOS Safari still ships no CSS ``overflow-anchor``, so a
  // late-loading image would otherwise shift the page out from under the reader.
  const anchorRef = useRef<{ el: HTMLElement; top: number } | null>(null);
  const lastSessionRef = useRef<string | null>(null);
  const [showJump, setShowJump] = useState(false);
  // Whether the transcript is actually scrollable. Measured by the same
  // ResizeObserver that owns the anchor restore, so it needs no observer of its
  // own and is fresh in the commit that changes either side of the comparison.
  const [historyOverflows, setHistoryOverflows] = useState(false);
  // Surfaces a failed older-page fetch. Without it the spinner simply vanishes,
  // which is indistinguishable from reaching the start of history — and a failure
  // adds no content, so nothing moves and the trigger below has no change to react
  // to. The explicit retry is the affordance for a reader who stays put.
  const [olderLoadFailed, setOlderLoadFailed] = useState(false);
  // The older-page trigger below is a question about the PRESENT, and every one
  // of its inputs has to be able to re-ask it. The sentinel's intersection does
  // so by itself, and so does anything the component renders from — props and
  // state re-create the observer through the effect's dependency list. A ref
  // does not: it changes silently, so a guard that goes false behind a ref is a
  // deadlock exactly like a latch that never re-arms, which is the defect this
  // change exists to remove, one layer down.
  //
  // So the only ref the trigger reads is the intersection itself, which arrives
  // with its own re-ask. Where the scroll handler and the ResizeObserver also
  // need to read a guard synchronously mid-layout, the ref is kept as a shadow
  // of the state and written ONLY through the setter beside it; where a prop
  // already says the same thing, the prop is read directly rather than mirrored.
  // What matters is that the trigger reads reactive values, so
  // react-hooks/exhaustive-deps fails the build on an input left out of the
  // dependency list — completeness enforced rather than remembered.

  // Whether an older page WE started is still outstanding. ``loadingOlder``
  // reports the same thing but lags: it only arrives after ChatPage has
  // re-rendered, and until it does the trigger would happily start a second
  // request for the page already on the wire. Owning it here closes that window,
  // and settling is also the honest moment to re-ask — a page has landed, so the
  // level condition has a new answer.
  const [loadInFlight, setLoadInFlight] = useState(false);
  const [followingTail, setFollowingTailState] = useState(true);
  const setFollowingTail = useCallback(
    (next: boolean) => {
      pinnedRef.current = next;
      setFollowingTailState(next);
    },
    [pinnedRef],
  );
  const loadOlderRef = useRef(onLoadOlder);
  const reloadLatestRef = useRef(onReloadLatest);
  // Older pages are triggered by whether a zero-height sentinel at the head of the
  // transcript is within OLDER_TRIGGER_BAND_PX of the viewport top — a question
  // about the PRESENT, which the browser re-answers on every geometry change.
  //
  // What this replaces: a latch that disarmed on trigger and re-armed only from a
  // scroll event that had settled for 150ms with ``scrollTop > 300``. Both halves
  // describe a FUTURE event, and the reader most likely to want another page —
  // parked at the very top — can produce neither: an upward gesture at scrollTop 0
  // moves nothing, so the browser emits no scroll event at all. One fling that
  // rode past the post-load anchor restore back to the top therefore left paging
  // dead — no spinner, no request, older messages still on the server — until the
  // reader happened to scroll >300px back down and up again.
  //
  // Cascade prevention (the reason that latch existed) no longer needs a position
  // threshold: a successful page prepends content and the anchor restore pushes
  // the sentinel out of the band, so the condition goes false on its own. If it is
  // still true afterwards, the reader really is still at the top of the loaded
  // window, and loading again is the correct answer rather than a cascade.
  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const atTopRef = useRef(false);

  useEffect(() => {
    loadOlderRef.current = onLoadOlder;
  }, [onLoadOlder]);

  useEffect(() => {
    reloadLatestRef.current = onReloadLatest;
  }, [onReloadLatest]);

  // Whenever the loaded window stops reaching the live tail — a search deep-link
  // window OR the retained-window cap detaching the tail while the reader is
  // scrolled up — force the jump-to-latest control visible. handleScroll only
  // recomputes ``showJump`` on scroll events, so a detach with no subsequent
  // scroll (the cap dropping an incoming row) would otherwise leave the reader
  // with no way back to the live tail until they scroll again.
  useEffect(() => {
    if (needsLatestReload) setShowJump(true);
  }, [needsLatestReload]);

  // The reply arrives atomically as a persisted ``result`` row (no streaming
  // card), so the thinking bubble shows for the whole gap between send and
  // reply. Hide it the moment the last row is a fresh agent terminal — a
  // successful ``result``, a legacy ``error``, or a structured backend-failure
  // ``notify`` all end the visible response. ``working`` itself still settles
  // only from turn.end or the authoritative turn-state poll, so a late row from
  // an older Turn cannot clear Stop for a newer one.
  const lastIsAgentTerminal =
    messages.length > 0 && isTerminalAgentMessage(messages[messages.length - 1]);
  // The running Activity card replaces the ThinkingBubble. It renders only while a
  // turn is in flight (``working``) AND the live buffer is non-empty and current-gen
  // (round-4 clause). Gating on ``working`` makes a stale buffer — e.g. one left by
  // a dropped turn.end that the idle poll recovered — invisible by construction.
  const liveActive = !!activity?.enabled && activity.liveRows.length > 0;
  const showActivityCard = shouldShowRunningCard(!!activity?.enabled, working, activity?.liveRows.length ?? 0);
  const showThinking = working && !lastIsAgentTerminal && !liveActive;
  // Render one settled-turn chip (before/after its anchor message, or at the top).
  // Plain render helper (not a component) so it stays referentially simple.
  const renderActivityChip = (group: ActivityGroup) =>
    activity ? (
      <ActivityChip
        key={group.id}
        group={group}
        expanded={!!activity.expanded[group.id]}
        loading={!!activity.loading[group.id]}
        error={!!activity.error[group.id]}
        onToggle={() => activity.onToggleGroup(group)}
        onRetry={() => activity.onRetryGroup(group)}
        showToolCalls={activity.showToolCalls}
        onToggleTools={activity.onToggleTools}
      />
    ) : null;
  const empty = messages.length === 0 && !working;
  // The end-of-history line answers "why did paging stop?" — a question only a
  // reader who actually scrolled can have asked. A chat that fits the viewport
  // never paged, so there the same line is noise rather than an answer.
  const atHistoryStart = !hasOlder && historyOverflows;

  // Capture the topmost (partly) visible row as the restore anchor. Viewport-
  // relative rects keep this correct regardless of the scroll container's padding;
  // it breaks at the first visible row, so the common case (reading near the top of
  // the loaded window) is a couple of reads. Called from the scroll handler while
  // the user is reading history, so the anchor is always fresh when a resize lands.
  // ``pickScrollAnchor`` owns which elements qualify: the chrome rendered above
  // ``messages.map`` below is disqualified, because a prepended page lands beneath
  // it and it therefore restores nothing (see the module's own note).
  const captureAnchor = useCallback(() => {
    // A programmatic jump is in flight — don't record an anchor mid-jump (the
    // restore would later snap back to it and undo the jump).
    if (suppressAnchorRef.current) return;
    const el = scrollRef.current;
    const content = contentRef.current;
    if (!el || !content) return;
    anchorRef.current = pickScrollAnchor(
      Array.from(content.children) as HTMLElement[],
      el.getBoundingClientRect().top,
    );
  }, []);

  // Jump to the exact bottom and resume following. Instant, not smooth: a smooth
  // glide emits intermediate scroll events that would flip the pin off mid-flight
  // and, if content grows during the glide, land short of the true bottom.
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setFollowingTail(true);
    anchorRef.current = null;
    setShowJump(false);
  }, [setFollowingTail]);

  // Jump-to-latest handler behind the down-arrow button. A search result that
  // was not already loaded installs a centered historical window; its loaded
  // bottom is not necessarily the live tail. In that state, ask ChatPage to swap
  // in a fresh tail window first, then scroll after the rows commit.
  const jumpToLatest = useCallback(() => {
    if (!needsLatestReload) {
      scrollToBottom();
      return;
    }
    void reloadLatestRef.current().then((installed) => {
      if (installed) requestAnimationFrame(() => scrollToBottom());
    });
  }, [needsLatestReload, scrollToBottom]);

  // The one path that starts an older-page load, so the sentinel trigger and the
  // retry affordance cannot drift apart on how a failure is recorded.
  const runLoadOlder = useCallback(() => {
    setLoadInFlight(true);
    setOlderLoadFailed(false);
    void Promise.resolve(loadOlderRef.current()).then((ok) => {
      setLoadInFlight(false);
      // A failed page adds no content, so the sentinel stays exactly where it
      // was and every later re-evaluation would see the same "reader wants more"
      // answer and re-ask — a retry storm against a server that is still
      // failing. Recording the failure both holds the automatic trigger off and
      // offers the retry line, which is the way forward for a reader who stays
      // put.
      //
      // Only if they DID stay put. A reader who left the band while the request
      // was in flight never saw this failure, and latching it behind their back
      // would refuse the automatic retry they are owed on returning — the exit
      // that would have cleared it happened before there was anything to clear.
      if (ok === false && atTopRef.current) setOlderLoadFailed(true);
    });
  }, []);

  // Start an older page when the sentinel is in the band and there is more to
  // load. Reads props directly — the effect below re-creates the observer when
  // they change — so it can never act on a stale snapshot.
  const maybeLoadOlder = useCallback(() => {
    if (!atTopRef.current || !hasOlder || loadingOlder || loadInFlight || olderLoadFailed) return;
    // Sentinel in view is only a REQUEST for older history if the reader went
    // looking for it. A transcript that fits its viewport — tall display, zoomed
    // out, enlarged window — puts the sentinel in the band while the reader is
    // still following the live tail, and paging there would walk backwards
    // through history nobody asked to see, on open and on every resize.
    if (followingTail) return;
    // A pending deep-link jump owns scrollTop, and the anchor restore is
    // suppressed along with it. Prepending under the jump would have nothing
    // holding the reader's row in place. A jump that lands near the head of the
    // loaded window arrives IN the band, so this is a real state to leave rather
    // than a momentary one — and ``jumpTarget`` IS that state: the effect below
    // acks the jump at the same moment it lifts suppression, which clears the
    // prop and re-asks. Reading it directly is why there is no mirror to keep in
    // step, and it holds from the moment the jump is requested rather than from
    // the frame the effect happens to run in.
    if (jumpTarget) return;
    runLoadOlder();
  }, [
    followingTail,
    hasOlder,
    jumpTarget,
    loadInFlight,
    loadingOlder,
    olderLoadFailed,
    runLoadOlder,
  ]);

  // Older-page trigger. The observer answers "is the reader within
  // OLDER_TRIGGER_BAND_PX of the top of the loaded window?" and re-answers on
  // every geometry change the browser already tracks. Every OTHER input — more
  // history to fetch, a page already in flight, following the tail, a jump
  // holding scrollTop, a failure already recorded — is not geometry, so it
  // arrives as a dep of ``maybeLoadOlder``: re-creating the observer re-observes
  // the sentinel, and ``observe()`` always reports the CURRENT state instead of
  // waiting for the next crossing. That is what makes the loader level-triggered
  // in ALL of its inputs rather than only in the one the browser watches.
  // In particular a settled page re-evaluates the settled geometry (React has
  // committed the prepended rows and the ResizeObserver has restored the anchor
  // by the time this passive effect runs), so a reader who is still at the top
  // gets the next page without having to produce a scroll event they may be
  // unable to produce at all. That edge is ``loadInFlight``, which we own, not
  // the ``loadingOlder`` prop, which only mirrors it.
  // ``empty`` is in the deps for the same reason as the ResizeObserver below: the
  // scroll container mounts when the empty state goes away.
  useEffect(() => {
    const root = scrollRef.current;
    const sentinel = topSentinelRef.current;
    if (!root || !sentinel) return;
    const io = new IntersectionObserver(
      (entries) => {
        const atTop = entries[entries.length - 1]?.isIntersecting ?? false;
        atTopRef.current = atTop;
        // Leaving the band is the reader telling us they moved on. Drop the
        // failure latch so returning to the top retries once, instead of leaving
        // the retry line as the only way forward for the rest of the session.
        if (!atTop) setOlderLoadFailed(false);
        maybeLoadOlder();
      },
      { root, rootMargin: `${OLDER_TRIGGER_BAND_PX}px 0px 0px 0px` },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [maybeLoadOlder, empty]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    // Small tolerance keeps us "following" through sub-pixel rounding; a
    // historical search window cannot be considered pinned to live tail until
    // the explicit latest reload succeeds.
    const pinned = distance < 80 && !needsLatestReload;
    setFollowingTail(pinned);
    setShowJump(distance > 240 || needsLatestReload);
    // Only track an anchor while reading history; following needs none (the bottom
    // is free to grow). Re-capturing here keeps it current as the user scrolls.
    if (pinned) anchorRef.current = null;
    else captureAnchor();
    // Older-page loading is deliberately NOT triggered here: scroll events are the
    // one thing a reader already at the top cannot produce. See the sentinel
    // observer above.
  };

  // Open each session pinned to the latest message (instant, no animation) —
  // opening from the inbox should land on what just arrived.
  useEffect(() => {
    if (lastSessionRef.current === session.id) return;
    lastSessionRef.current = session.id;
    setFollowingTail(true);
    anchorRef.current = null;
    setShowJump(false);
    // Paging state belongs to the session that was open, not to the transcript:
    // the component stays mounted across a switch, so a load that failed in the
    // previous session would otherwise greet the next one with a retry line for
    // a page it never asked for, and hold its loader off with it.
    setOlderLoadFailed(false);
    const id = requestAnimationFrame(() => scrollToBottom());
    return () => cancelAnimationFrame(id);
  }, [session.id, scrollToBottom, setFollowingTail]);

  // Deep-link jump (P5): once ChatPage has put the target message into
  // ``messages`` (either it was already loaded or the around-window was fetched
  // and swapped in), scroll it to center and ack the jump. Keyed on
  // [jumpTarget, messages] so it fires after the window commits to the DOM; the
  // ``data-message-id`` lookup runs in the next frame so the row is laid out.
  // The suppression flag stops the iOS scroll-anchor from snapping back. We
  // unpin (we're jumping INTO history, not following the tail) and clear the
  // anchor so the ResizeObserver doesn't immediately re-pin/restore once the
  // suppression lifts.
  useEffect(() => {
    if (!jumpTarget) return;
    const el = scrollRef.current;
    if (!el) return;
    // If the target is the newest loaded row AND we're genuinely at the live tail
    // (not a centered historical window), pin to the bottom and keep following
    // instead of centering+unpinning. The highlight still applies via
    // ``highlightedId``, independent of the scroll.
    if (!needsLatestReload && messages.length > 0 && messages[messages.length - 1]?.id === jumpTarget) {
      const rafTail = requestAnimationFrame(() => {
        scrollToBottom();
        onJumpHandled();
      });
      return () => cancelAnimationFrame(rafTail);
    }
    let raf2 = 0;
    suppressAnchorRef.current = true;
    anchorRef.current = null;
    const raf1 = requestAnimationFrame(() => {
      // Unpin with the scroll that actually leaves the tail, not a frame ahead
      // of it: until this runs the transcript has not moved anywhere.
      setFollowingTail(false);
      const row = el.querySelector(`[data-message-id="${CSS.escape(jumpTarget)}"]`);
      if (row) {
        row.scrollIntoView({ block: 'center' });
        setShowJump(true); // not at the bottom anymore — offer the way back down
      }
      // Re-capture the anchor at the jumped-to position on the NEXT frame (after
      // the scroll lands), then lift suppression — so a later image/resize keeps
      // the jumped-to row stable via the normal anchor path instead of drifting.
      raf2 = requestAnimationFrame(() => {
        suppressAnchorRef.current = false;
        captureAnchor();
        onJumpHandled();
      });
    });
    return () => {
      cancelAnimationFrame(raf1);
      if (raf2) cancelAnimationFrame(raf2);
      suppressAnchorRef.current = false;
    };
  }, [
    jumpTarget,
    messages,
    needsLatestReload,
    captureAnchor,
    onJumpHandled,
    scrollToBottom,
    setFollowingTail,
  ]);

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
      // Overflow is a comparison between the two boxes, so BOTH are observed: the
      // content grows as pages load, and the viewport shrinks under a composer that
      // has expanded, an on-screen keyboard, or a resized window. Recomputed ahead
      // of the anchor branches below so no early return can leave it stale.
      setHistoryOverflows(el.scrollHeight > el.clientHeight + 1);
      // A programmatic jump owns scrollTop right now — neither pin-to-bottom nor
      // anchor-restore should move it, or it would fight the jump.
      if (suppressAnchorRef.current) return;
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
    // The viewport shrinking under the reader is a resize too: pin-to-bottom has to
    // re-pin and the anchor has to hold, exactly as when the content itself grew.
    ro.observe(el);
    return () => ro.disconnect();
  }, [empty]);

  if (empty) {
    // Even with no transcript-visible messages, a session can have pending vault requests —
    // render the footer (cards + observer) below the empty state so they aren't invisible.
    const emptyBody =
      isForkedSession && forkSourceSessionId ? (
        <>
          <div className="mx-auto flex w-full max-w-[1080px] flex-col gap-3">{forkSourceBanner}</div>
          <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center text-muted">
            <GitFork className="size-8 opacity-70" />
            <div className="max-w-[360px] text-[13px] font-semibold text-foreground">{t('chat.forkedEmptyTitle')}</div>
            <div className="max-w-[440px] text-[12px] leading-relaxed">{t('chat.forkedEmptyBody')}</div>
          </div>
        </>
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center text-muted">
          <MessageSquare className="size-8 opacity-60" />
          <div className="text-[13px]">{t('chat.transcriptEmpty')}</div>
        </div>
      );
    return (
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4 py-5 [overflow-anchor:none] md:px-8">
        {emptyBody}
        {footer ? <div className="mx-auto mt-3 w-full max-w-[1080px] shrink-0">{footer}</div> : null}
      </div>
    );
  }
  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {!readOnly && (
        <SelectionQuoteToolbar
          containerRef={scrollRef}
          // Both write actions are omitted rather than offered just to fail —
          // see transcriptSelectionActions. On an archived session that leaves only
          // the touch Copy fallback, and on desktop the toolbar renders nothing.
          onQuote={selectionActions.quote ? onQuoteSelection : undefined}
          // Forking needs a bound native session (mirrors the sidebar's fork gate).
          onAskInNew={selectionActions.askInNew ? onAskInNewSession : undefined}
        />
      )}
      <div ref={scrollRef} onScroll={handleScroll} className="min-h-0 flex-1 overflow-y-auto px-4 py-5 [overflow-anchor:none] md:px-8">
        {/* Marker for the very top of the loaded window: whether it is in view IS
            the "load older" condition (see the observer above). Outside
            ``contentRef`` so it joins neither the column's gap spacing nor the
            children pickScrollAnchor searches. */}
        <div ref={topSentinelRef} aria-hidden className="h-px" />
        <div ref={contentRef} className="mx-auto flex w-full max-w-[1080px] flex-col gap-3">
          {forkSourceBanner}
          {/* One slot at the head of the history for every way paging can end, so
              each outcome resolves in place instead of the top twitching: still
              loading, failed (and retryable), or nothing older left. */}
          {loadingOlder ? (
            <div
              role="status"
              aria-label={t('chat.loadingOlder')}
              className="flex h-8 items-center justify-center text-muted"
            >
              <Loader2 className="size-4 animate-spin" />
            </div>
          ) : olderLoadFailed ? (
            <button
              type="button"
              onClick={runLoadOlder}
              className="flex h-8 items-center justify-center text-[12px] text-muted hover:text-foreground"
            >
              {t('chat.olderLoadFailed')}
            </button>
          ) : atHistoryStart ? (
            <div className="flex h-8 items-center justify-center text-[12px] text-muted">
              {t('chat.noEarlierMessages')}
            </div>
          ) : null}
          {/* Degenerate null-anchor groups render at the TOP (never the tail). */}
          {activity?.enabled && activity.topGroups.map((group) => renderActivityChip(group))}
          {messages.map((message) => {
            // Agent Activity chips positioned relative to THIS row: 'before' it
            // (done/failed, hugging the reply from above) and 'after' it (interrupted,
            // just below the turn's trigger).
            const before = activity?.enabled ? activity.beforeAnchor.get(message.id) : undefined;
            const after = activity?.enabled ? activity.afterAnchor.get(message.id) : undefined;
            return (
              <Fragment key={message.id}>
                {before?.map((group) => renderActivityChip(group))}
                <MessageRow
                  message={message}
                  session={session}
                  agentDisplayName={agentDisplayName}
                  messageFontSize={messageFontSize}
                  onQuickReply={onQuickReply}
                  vaultRequests={provisionRequestsByMessage.get(message.id)}
                  onVaultRequestResolved={onVaultRequestResolved}
                  onOpenLocalFile={openLocalFile}
                  readOnly={readOnly}
                  highlighted={message.id === highlightedId}
                />
                {after?.map((group) => renderActivityChip(group))}
              </Fragment>
            );
          })}
          {/* The transcript TAIL is reserved exclusively for the live running card
              (or the ThinkingBubble fallback) — never a settled/interrupted chip. */}
          {showActivityCard && activity ? (
            <ActivityCard
              rows={activity.liveRows}
              startedAtMs={activity.liveStartedAt}
              expanded={activity.cardExpanded}
              onToggleExpanded={activity.onToggleCard}
              showToolCalls={activity.showToolCalls}
              onToggleTools={activity.onToggleTools}
              onDisableActivity={activity.onDisable}
            />
          ) : showThinking ? (
            <ThinkingBubble
              session={session}
              agentDisplayName={agentDisplayName}
              onShowActivity={!activity?.enabled ? activity?.onEnable : undefined}
            />
          ) : null}
          {footer}
        </div>
      </div>
      {/* Jump-to-latest: appears after scrolling up a clear distance, returns to
          the bottom on click. Centered just above the compose bar. */}
      {showJump && (
        <Button
          type="button"
          variant="secondary"
          size="icon"
          onClick={jumpToLatest}
          aria-label={t('chat.scrollToBottom')}
          className="absolute bottom-3 left-1/2 size-9 -translate-x-1/2 rounded-full border-border-strong shadow-lg"
        >
          <ChevronDown className="size-4" />
        </Button>
      )}
    </div>
  );
};

const formatForkSourceLabel = (sourceSessionId: string, sourceTitle: string | null): string => {
  if (sourceTitle) return sourceTitle;
  if (sourceSessionId.length <= 14) return sourceSessionId;
  return `${sourceSessionId.slice(0, 8)}...${sourceSessionId.slice(-4)}`;
};

const ForkSourceBanner: React.FC<{ sourceSessionId: string; sourceTitle: string | null }> = ({
  sourceSessionId,
  sourceTitle,
}) => {
  const { t } = useTranslation();
  const sourceLabel = formatForkSourceLabel(sourceSessionId, sourceTitle);
  return (
    <div className="flex w-full justify-center">
      <Link
        to={`/chat/${encodeURIComponent(sourceSessionId)}`}
        className="inline-flex max-w-full items-center gap-2 rounded-full border border-cyan/30 bg-cyan/[0.08] px-3 py-1.5 text-[12px] text-cyan-ink transition-colors hover:border-cyan/50 hover:bg-cyan/[0.12]"
      >
        <GitFork className="size-3.5 shrink-0" />
        <span className="shrink-0">{t('chat.forkedFromPrefix')}</span>
        <span className="min-w-0 truncate font-semibold text-foreground">{sourceLabel}</span>
      </Link>
    </div>
  );
};

// Shown while a turn is in flight but the reply hasn't landed yet — a left
// agent bubble with three dots that fade in sequence (``.vr-typing-dot``
// keyframes in index.css), so the user gets immediate feedback a reply is
// coming (feedback #1).
export const ThinkingBubble: React.FC<{
  session: WorkbenchSession;
  agentDisplayName: string | null;
  onShowActivity?: () => void;
}> = ({ session, agentDisplayName, onShowActivity }) => {
  const { t } = useTranslation();
  const dots = (
    <div className="flex items-center gap-1 py-0.5">
      <span className="vr-typing-dot size-1.5 rounded-full bg-mint" />
      <span className="vr-typing-dot size-1.5 rounded-full bg-mint [animation-delay:0.2s]" />
      <span className="vr-typing-dot size-1.5 rounded-full bg-mint [animation-delay:0.4s]" />
    </div>
  );
  return (
    <div className="flex w-full justify-start">
      <div className="group/message flex max-w-[min(92%,860px)] flex-col items-start gap-1">
        <div className="flex items-center gap-2 px-0.5">
          <RoleAvatar tone="mint"><Bot /></RoleAvatar>
          <span className="text-[11px] font-medium text-muted">
            {agentDisplayName || session.agent_name || t('chat.thinking')}
          </span>
        </div>
        {onShowActivity ? (
          <button
            type="button"
            onClick={onShowActivity}
            aria-label={t('chat.agentActivity.enable')}
            title={t('chat.agentActivity.enable')}
            className="w-fit cursor-pointer rounded-2xl rounded-tl-md border border-mint/25 bg-mint/[0.09] px-3.5 py-2.5 transition-colors hover:border-mint/45 hover:bg-mint/[0.14]"
          >
            {dots}
          </button>
        ) : (
          <div className="w-fit rounded-2xl rounded-tl-md border border-mint/25 bg-mint/[0.09] px-3.5 py-2.5">
            {dots}
          </div>
        )}
      </div>
    </div>
  );
};

type MessageRowProps = {
  message: WorkbenchMessage;
  session: WorkbenchSession;
  agentDisplayName?: string | null;
  messageFontSize: number;
  onQuickReply?: (messageId: string, choice: string) => boolean | void | Promise<boolean | void>;
  vaultRequests?: VaultRequest[];
  onVaultRequestResolved?: () => void;
  onOpenLocalFile?: (target: LocalFileLinkTarget) => void | Promise<void>;
  // Archived session: the row still renders in full — including the quick-reply
  // group and which option was chosen, which is part of the transcript — but the
  // group is frozen, so an old quick reply can no longer POST a doomed message.
  readOnly?: boolean;
  // When true, this row was the deep-link jump target — wrap it in a brief mint
  // fade (``msg-highlight``). Drives the only visual difference for the matched
  // message; included in the memo's shallow compare so the highlight on/off
  // re-renders just this row.
  highlighted?: boolean;
};

// Memoized so a transcript re-render that doesn't touch THIS row — the scroll
// handler's showJump toggle, the working/thinking state, a sibling message
// arriving — skips it entirely. Without this, every such re-render re-runs
// <Markdown>, which (via react-markdown) remounts the row's <img>s; a remounted
// image is re-decoded, which is what flickers the bubble on iOS Safari while
// scrolling. The props are referentially stable per row (the message/session
// objects only change when that row's data does, and onQuickReply is a
// useCallback), so the default shallow compare is correct here.
// Exported for the read-only regression test (ChatArchivedReadOnly.test.tsx),
// which renders a single row rather than mounting the whole page.
export const MessageRow = memo(function MessageRow({
  message,
  session,
  agentDisplayName,
  messageFontSize,
  onQuickReply,
  vaultRequests,
  onVaultRequestResolved,
  onOpenLocalFile,
  readOnly,
  highlighted,
}: MessageRowProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  // Harness rows are collapsed by default; this tracks the per-row expand state.
  const [expanded, setExpanded] = useState(false);

  // Deep-link jump target dressing applied to every row's outer wrapper:
  //  - ``data-message-id`` lets the transcript locate the row to scroll to.
  //  - ``msg-highlight`` paints the brief mint fade (design.pen tBlve).
  // Each branch composes this onto its own ``justify-*`` so alignment is kept.
  const rowClass = (extra: string) => clsx('flex w-full', extra, highlighted && 'msg-highlight');

  // Which card family draws this row — one decision, made once, in a pure mapper
  // (``chatRowKind``) so the ordering between the families is testable without
  // mounting the page. Everything below reads the answer; nothing re-derives it.
  const row = chatRowKind(message);
  const isNotify = row.kind === 'notify';
  const isAgent = row.kind === 'agent';
  const isBoundary = row.kind === 'boundary';
  // ...and, separately, who wrote it. Only the agent's own words may carry the
  // agent-authored Markdown affordances, and its reverse annotation is still its
  // own words even though a different card draws it.
  const agentAuthored = isAgentAuthored(message);
  const isHarness = row.kind === 'harness';
  const isUser = row.kind === 'user';
  const isVaultNotification = isNotify && isVaultCallback(message);
  // Trigger-message provenance click-through (contract A9a/A9b): agent-callback
  // rows link to the source session's chat; task/watch rows to the Harness view.
  const triggerLink = isHarness ? chatTriggerLink(message, t('chat.source.agentFallback')) : null;
  const vaultStatusKey = isVaultCallback(message) ? vaultCallbackStatusKey(message) : null;
  const messageFontStyle = { fontSize: `${normalizeChatMessageFontSize(messageFontSize)}px` };
  const resultPresentation = resultFooterParts(message);

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
  const qr = agentAuthored
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
        // Archived: keep the record (which options were offered, which was
        // chosen) but lock the group so no click can start a rejected send.
        readOnly={readOnly}
        onChoose={(choice) => onQuickReply(message.id, choice)}
      />
    ) : null;
  // Unlike a `$<NAME>` marker in the reply text, attached requests are live
  // pending state, not authored transcript content. Archive expires that state,
  // so a stale tab must withdraw the card instead of preserving a false action.
  const vaultRequestsNode = !readOnly && vaultRequests?.length && onVaultRequestResolved ? (
    <div className="flex w-full flex-col gap-2 pt-1">
      {vaultRequests.map((request) => (
        <VaultRequestCard key={request.id} request={request} onResolved={onVaultRequestResolved} />
      ))}
    </div>
  ) : null;

  // Agent / system replies AND the user's own messages render as markdown (users
  // routinely type lists / code / **emphasis** and expect it formatted). Only
  // Every message body renders as Markdown — including the expanded harness row
  // (scheduled task / watch / webhook prompt), which used to stay verbatim.
  // Harness prompts and the user's own messages keep soft breaks so their
  // original line breaks stay visible (a harness prompt often mixes authored
  // Markdown with line-oriented waiter output); agent/system replies are
  // authored Markdown and must not get stray hard breaks.
  const bodyNode = resultPresentation.body ? (
    <Markdown
      content={resultPresentation.body}
      // An annotation the user typed is the user's own words (rule 05), so it
      // keeps their line breaks exactly as the ordinary user bubble does.
      softBreaks={isUser || isHarness || (row.kind === 'annotation' && row.annotation.direction === 'user')}
      references={(message.content as { references?: MentionReference[] } | null)?.references}
      // Only the agent's own words may render `$<NAME>` as an interactive secret-input card;
      // user/harness/system bubbles with the marker stay plain text (no false "agent asked"
      // card). Keyed to authorship, not to the card family, so the agent's reverse annotation
      // keeps the card it had before that row had its own type.
      secretRequests={agentAuthored}
      localFileWorkdir={agentAuthored ? session.workdir : undefined}
      onOpenLocalFile={agentAuthored ? onOpenLocalFile : undefined}
      // …and on an archived transcript the card is locked: archiving EXPIRED the
      // session's provision requests, so an enabled Provide button would tell the
      // reader an agent is waiting for this secret when none is.
      readOnly={readOnly}
      className="vr-markdown--inherit-size"
    />
  ) : !resultPresentation.footer && drawsEmptyBodyPlaceholder(row, messageAttachments.length > 0) ? (
    <div className="text-[13px] text-muted">—</div>
  ) : null;

  // Timestamp is metadata, not content: hidden by default, revealed while the
  // pointer is over the message OR focus moves into it (a keyboard user tabbing
  // to a link/button inside; doesn't add a tab stop on its own). The column
  // carries a NAMED group (``group/message``) so this reveal can't collide with
  // the unnamed ``group-hover`` ChatImage uses for its own overlay button.
  // Coarse pointers (touch) have no hover, so keep it always visible there.
  // Keep the reveal immediate: scrolling moves rows beneath a stationary pointer,
  // and an opacity transition would keep restyling and painting after each crossing.
  const time = (
    <span
      className="inline-flex max-w-full flex-wrap items-center gap-x-1.5 gap-y-0.5 px-1 font-mono text-[10px] text-muted opacity-0 group-hover/message:opacity-100 group-focus-within/message:opacity-100 pointer-coarse:opacity-100"
    >
      <span className="whitespace-nowrap">{formatLocalDateTime(message.created_at)}</span>
      {resultPresentation.footer ? <span className="min-w-0 break-words">{resultPresentation.footer}</span> : null}
    </span>
  );

  // ----- Annotation: the Show Page card, sided by direction (design.pen m31JWV)
  if (row.kind === 'annotation') {
    return (
      <AnnotationMessage
        messageId={message.id}
        view={row.annotation}
        body={bodyNode}
        attachments={attachmentsNode}
        time={time}
        bodyStyle={messageFontStyle}
        rowClass={rowClass}
      />
    );
  }

  // ----- Notify: compact gold pill, left-aligned (a status marker) -----
  if (isNotify) {
    return (
      <div data-message-id={message.id} className={rowClass('justify-start')}>
        <div className="group/message flex max-w-[min(92%,860px)] flex-col items-start gap-1">
          <div className="inline-flex w-fit max-w-full items-start gap-1.5 rounded-2xl rounded-tl-md border border-gold/30 bg-gold/[0.08] px-3 py-1.5 text-[12px] text-gold-ink">
            <Bell className="mt-px size-3 shrink-0" />
            <span className="min-w-0 break-words">
              <span className="font-semibold">{t(isVaultNotification ? 'chat.source.vault' : 'chat.notifyLabel')}</span>
              {vaultStatusKey && (
                <span className="font-normal text-gold-ink/80"> · {t(vaultStatusKey)}</span>
              )}
              {resultPresentation.body && (
                <span className="font-normal text-gold-ink/80"> · {resultPresentation.body}</span>
              )}
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
      <div data-message-id={message.id} className={rowClass('justify-end')}>
        <div className="group/message flex max-w-[min(92%,860px)] flex-col items-end gap-1">
          <div className={USER_BUBBLE} style={messageFontStyle}>
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
      <div data-message-id={message.id} className={rowClass('justify-start')}>
        <div className="group/message flex max-w-[min(92%,860px)] flex-col items-start gap-1">
          <div className="flex items-center gap-2 px-0.5">
            <RoleAvatar tone="cyan"><Clock /></RoleAvatar>
            {/* Label and source title read as one phrase ("From · <title>"), so they
                sit in a tight cluster; the parent gap only spaces avatar ↔ cluster. */}
            <span className="inline-flex min-w-0 items-center gap-1">
              {triggerLink?.kind === 'harness' ? (
                // A9b: task/watch label deep-links to the Harness filtered view.
                <button
                  type="button"
                  onClick={() => navigate(triggerLink.to)}
                  className="inline-flex items-center gap-1 text-[11px] font-medium text-cyan-ink hover:underline"
                >
                  {t(harnessChipLabelKey(message))}
                  <ArrowUpRight className="size-3 shrink-0" />
                </button>
              ) : (
                <span className="shrink-0 text-[11px] font-medium text-cyan-ink">
                  {t(harnessChipLabelKey(message))}
                </span>
              )}
              {vaultStatusKey && (
                <>
                  <span className="shrink-0 text-[11px] text-muted">·</span>
                  <span className="shrink-0 text-[11px] text-muted">{t(vaultStatusKey)}</span>
                </>
              )}
              {triggerLink?.kind === 'source' && (
                // A9a: agent-callback shows the SOURCE session + links to its chat.
                <>
                  <span className="shrink-0 text-[11px] text-muted">·</span>
                  <button
                    type="button"
                    onClick={() => navigate(triggerLink.to)}
                    className="inline-flex min-w-0 items-center gap-1 text-[11px] font-medium text-cyan-ink hover:underline"
                  >
                    <span className="min-w-0 truncate">{triggerLink.label}</span>
                    <ArrowUpRight className="size-3 shrink-0" />
                  </button>
                </>
              )}
            </span>
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

  // ----- Agent / boundary / system: left-aligned bubble with identity header -----
  const agentIdentity = isAgent || isBoundary;
  const name = agentIdentity
    ? agentDisplayName || session.agent_name || message.author_name
    : message.author_name;
  return (
    <div data-message-id={message.id} className={rowClass('justify-start')}>
      <div className="group/message flex max-w-[min(92%,860px)] flex-col items-start gap-1">
        <div className="flex items-center gap-2 px-0.5">
          <RoleAvatar tone={isAgent ? 'mint' : 'muted'}>{agentIdentity ? <Bot /> : <Info />}</RoleAvatar>
          {name && <span className="text-[11px] font-medium text-muted">{name}</span>}
        </div>
        {bodyNode || attachmentsNode ? (
          <div className={isAgent ? AGENT_BUBBLE : SYSTEM_BUBBLE} style={messageFontStyle}>
            {bodyNode}
            {attachmentsNode}
          </div>
        ) : null}
        {quickRepliesNode}
        {vaultRequestsNode}
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
        className="inline-flex items-center gap-1.5 text-[12px] text-cyan-ink hover:underline"
      >
        <ArrowLeft className="size-3.5" />
        {t('chat.back')}
      </button>
      <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
        {t('chat.missingSessionId')}
      </div>
    </div>
  );
};
