import { useCallback, useEffect, useId, useMemo, useRef, useState, useLayoutEffect, type CSSProperties, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ChevronUp, KeyRound, X } from 'lucide-react';

import { ApiError, useApi, type InboxSession, type RunningAgent, type VaultRequest, type WorkbenchMessage } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { Button } from '@/components/ui/button';
import { ChatImage } from '@/components/ui/chat-image';
import { FileCard } from '@/components/ui/file-card';
import { Markdown } from '@/components/ui/markdown';
import { QuickReplies } from '@/components/workbench/QuickReplies';
import { isComposingKey } from '@/lib/imeComposition';
import { useDocumentVisible, usePageActive } from '@/lib/pageActivity';
import { isDetachedCompletionMessage } from '@/lib/chatMessageTypes';
import { readMessageAttachments } from '@/lib/messageAttachments';
import { usePendingVaultRequests } from '@/lib/usePendingVaultRequests';
import { useVaultRequestRefresh } from '@/lib/useVaultRequestRefresh';
import { vaultRequestType } from '@/lib/vaultRequestPlacement';
import { referenceToMarker, type MentionReference, type SessionReference } from '@/lib/mentions';
import { ShowPageVoiceDictation } from '@/lib/showPageVoiceDictation';
import { VoiceTranscriptionError } from '@/lib/voiceTranscription';

import { PetAvatar, type PetPose } from './PetAvatar';
import { PetOpenButton } from './PetOpenButton';
import { REPLY_LINGER_RESUME_MS, useReplyLinger } from './useReplyLinger';
import { onPetHover, petBridge, sessionLink, vaultRequestLink, type PetLayout } from './petBridge';
import { petPanel, usePetPanel } from './petPanel';
import { petComposer, usePetComposer } from './petComposer';
import { petShell, useOnSummon, usePetBinding, usePetDismissed } from './petShell';
import { derivePetPose, derivePetState, latestExchange, latestFailureNotice, openQuickReplies } from './petState';
import { useConversationAgentSnapshot } from './useConversationAgentWorking';
import { usePetSession, type PetSurfaceEpoch } from './usePetSession';
import { usePetSetup } from './usePetSetup';
import { usePetReplyActivities } from './usePetReplyActivities';
import { FencedSource } from './fencedSource';
import './pet-mini.css';

const HOLD_MS = 320;
const DOUBLE_CLICK_DETECTION_MS = 450;
const DISMISS_EXIT_MS = 500;
const STOP_CONFIRM_MS = 3000;
const STOP_LINGER_MS = 2500;
const STACK_HEIGHT = 304; // 292px scroll box + 6px panel padding on each side.
/**
 * A scroll edge fade band (spec §7.5): its full height, and the mask's alpha
 * ramp across it as `[alpha, fraction of the band]` stops between transparent
 * at the edge and opaque at the band's inner end.
 */
type FadeProfile = { size: number; ramp: readonly (readonly [number, number])[] };
const CARD_FADE: FadeProfile = { size: 32, ramp: [] };
// The history is denser text, so its band is taller and darker at the edge.
const NO_REPLIES: ReadonlySet<string> = new Set();
const HISTORY_FADE: FadeProfile = { size: 48, ramp: [[0.12, 0.35], [0.55, 0.7]] };
// How close to the end of the history still counts as reading at the bottom.
const HISTORY_STICK_PX = 8;
const MAX_COLLAPSED_LAYERS = 3;
const MAX_COLLAPSED_COUNT = 99;
const EMPTY_INBOX: InboxSession[] = [];
const REFUSED_SEND_STATUSES = new Set([400, 403, 404, 409, 422]);
const sendRefused = (error: unknown) => error instanceof ApiError && REFUSED_SEND_STATUSES.has(error.status);

type StopPhase = 'confirm' | 'stopping' | 'stopped' | 'exited' | 'failed';
type PetCard = {
  id: string;
  sessionId: string;
  title: string;
  eyebrow: string;
  status: string;
  running: boolean;
  needsInput: boolean;
  hasUnread: boolean;
  canStop: boolean;
  canOpen: boolean;
  vaultRequestIds: string[];
};

type CardPresentation = { phase?: StopPhase; leaving?: boolean; retained?: PetCard };

const isRunning = (agent: RunningAgent) => agent.state === 'active';

/** Make an agent reply readable when it is shown inside a compact card. */
const collapseMarkdownToPlainText = (text: string): string => {
  const code: string[] = [];
  let nextCode = 0;
  const protectCode = (): string => `@@PETCODE${nextCode++}@@`;
  const plain = text
    .replace(/```[^\n]*\n([\s\S]*?)\n?```|~~~[^\n]*\n([\s\S]*?)\n?~~~/g, (_match, fencedBacktick: string, fencedTilde: string) => {
      const value = fencedBacktick ?? fencedTilde ?? '';
      code.push(value);
      return protectCode();
    })
    .replace(/`([^`\n]+)`/g, (_match, value: string) => {
      code.push(value);
      return protectCode();
    })
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/(^|\n)\s{0,3}(?:#{1,6}|>|[-*+]|\d+\.)[ \t]+/g, '$1')
    .replace(/~~([^~\n]+)~~/g, '$1')
    .replace(/\*\*\*([^*\n]+)\*\*\*/g, '$1')
    .replace(/___([^_\n]+)___/g, '$1')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1')
    .replace(/__([^_\n]+)__/g, '$1')
    .replace(/(?<!\w)\*([^*\n]+)\*(?!\w)/g, '$1')
    .replace(/(?<!\w)_([^_\n]+)_(?!\w)/g, '$1')
    .replace(/<!--[\s\S]*?-->|<\/?[a-z][a-z0-9-]*(?:\s+[^<>]*?)?\/?>/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return plain.replace(/@@PETCODE(\d+)@@/g, (_match, index: string) => code[Number(index)] ?? '');
};

function agentCard(
  agent: RunningAgent,
  unreadBySession: Record<string, number>,
  previewBySession: Record<string, string>,
  vaultRequests: VaultRequest[],
  stoppable: string[],
  fallback: string,
  t: (key: string, options?: Record<string, unknown>) => string,
): PetCard | null {
  if (!agent.session_id) return null;
  const project = agent.scope_display_name?.trim() || t('pet.unknownProject');
  const name = agent.agent_name?.trim() || agent.backend.trim() || t('pet.unknownAgent');
  const platform = agent.platform && agent.platform !== 'avibe'
    ? t(`pet.platform.${['slack', 'discord', 'telegram', 'feishu', 'lark', 'wechat'].includes(agent.platform) ? agent.platform : 'unknown'}`) : '';
  const needsInput = vaultRequests.some((request) => request.session?.id === agent.session_id);
  const hasUnread = (unreadBySession[agent.session_id] ?? 0) > 0;
  const running = isRunning(agent);
  const preview = collapseMarkdownToPlainText(previewBySession[agent.session_id] ?? '');
  const status = needsInput
    ? t('pet.needsAuthorization')
    : running
      ? t('pet.activityStatus.running')
      : hasUnread
        ? preview
        : '';
  return {
    id: agent.session_id,
    sessionId: agent.session_id,
    title: agent.title?.trim() || fallback,
    eyebrow: [platform, project, name].filter(Boolean).join(' · '),
    status,
    running,
    needsInput,
    hasUnread,
    // This snapshot defaults trigger_source to human; it carries no caller
    // lineage. Never infer a delegation to or from the personal agent.
    canStop: running && stoppable.includes(agent.session_id),
    canOpen: agent.openable_in_chat,
    vaultRequestIds: vaultRequests.filter((request) => request.session?.id === agent.session_id).map((request) => request.id),
  };
}

function MicIcon() {
  return (
    <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden>
      <rect x="5.5" y="1.8" width="5" height="8" rx="2.5" />
      <path d="M3.2 7.5a4.8 4.8 0 0 0 9.6 0M8 12.3v2" />
    </svg>
  );
}

function KeyboardIcon() {
  return (
    <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden>
      <rect x="1.5" y="4" width="13" height="8.5" rx="2" />
      <path d="M4 7h.01M6.5 7h.01M9 7h.01M11.5 7h.01M5 10h6" />
    </svg>
  );
}

function StopIcon() {
  return <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor" /></svg>;
}

function SendIcon() {
  return <svg viewBox="0 0 16 16" className="size-3.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" /></svg>;
}

export const PetPage: React.FC = () => {
  usePetBinding();
  const { setup, recheck } = usePetSetup();
  if (setup === 'checking') return null;
  if (setup === 'pending') return <PetSetupPending recheck={recheck} />;
  return <PetSurface />;
};

const PetSetupPending: React.FC<{ recheck: () => void }> = ({ recheck }) => {
  const { t } = useTranslation();
  useOnSummon(recheck);
  const { layout, awaiting } = usePetPanel();
  const rootRef = useRef<HTMLDivElement>(null);
  useHitRegions(rootRef);
  return (
    <div ref={rootRef} className="pet-mini-stage" data-pet-root data-edge={layout.panel_edge} data-layout-pending={awaiting || undefined}>
      <div className="pet-mini-setup" data-pet-hit>
        <p>{t('pet.setupPending')}</p>
        <Button size="sm" onClick={() => void petBridge.open('avibe://settings')}>{t('pet.openAvibe')}</Button>
      </div>
      <div className="pet-mini-avatar-wrap" data-pet-hit><PetAvatar pose="idle" /></div>
    </div>
  );
};

function useVisibleActivityActions(enabled: boolean, epoch: PetSurfaceEpoch, sessionIds: string, cancellableIds: string) {
  const api = useApi();
  const [value, setValue] = useState<{ epoch: PetSurfaceEpoch; sessionIds: string; cancellableIds: string; requests: VaultRequest[]; stoppable: string[] } | null>(null);
  const source = useMemo(() => new FencedSource<PetSurfaceEpoch, { requests: VaultRequest[]; stoppable: string[] }>({
    read: async () => {
      const ids: string[] = JSON.parse(sessionIds);
      const groups = await Promise.all(ids.map(async (id) => {
        const result = await api.getVaultRequests({ status: 'pending', session: id }, { handleError: false }).catch(() => ({ requests: [] }));
        return (result.requests ?? []).map((request) => ({
          ...request,
          session: request.session ?? { id, title: null, label: null, platform: null, scope_kind: null, is_workbench: false },
        }));
      }));
      const candidates: string[] = JSON.parse(cancellableIds);
      const stoppable = await Promise.all(candidates.map(async (id) => {
        try {
          const turn = await api.getTurnState(id, { handleError: false });
          return turn.in_flight === true ? id : null;
        } catch { return null; }
      }));
      return { requests: groups.flat(), stoppable: stoppable.filter((id): id is string => id !== null) };
    },
    apply: (key, { requests, stoppable }) => {
      if (key.isCurrent()) setValue({ epoch: key, sessionIds, cancellableIds, stoppable, requests: requests.filter((request) => vaultRequestType(request) !== 'other') });
    },
    fail: (key) => { if (key.isCurrent()) setValue(null); },
  }), [api, sessionIds, cancellableIds]);
  const initiallyRead = useRef(false);
  useEffect(() => {
    initiallyRead.current = false;
    source.setKey(enabled ? epoch : null);
    return () => source.setKey(null);
  }, [source, enabled, epoch]);
  const refresh = useCallback(() => {
    if (!enabled || !epoch.isCurrent()) return;
    initiallyRead.current = true;
    source.refresh();
  }, [source, enabled, epoch]);
  useVaultRequestRefresh(refresh);
  // The fallback hook performs the first visible read. When the bridge is
  // already healthy it does not poll, so a new source/epoch still needs one.
  useEffect(() => {
    if (!initiallyRead.current) refresh();
  }, [refresh]);
  const requests = enabled && value?.epoch === epoch && value.sessionIds === sessionIds ? value.requests : [];
  useEffect(() => {
    const nextExpiry = Math.min(...requests.map((request) => Date.parse(request.expires_at ?? '')).filter((time) => time > Date.now()));
    if (!Number.isFinite(nextExpiry)) return;
    const timer = window.setTimeout(refresh, Math.min(nextExpiry - Date.now() + 250, 2_000_000_000));
    return () => window.clearTimeout(timer);
  }, [requests, refresh]);
  return { requests, ready: !enabled || (value?.epoch === epoch && value.sessionIds === sessionIds), stoppable: enabled && value?.epoch === epoch && value.sessionIds === sessionIds && value.cancellableIds === cancellableIds ? value.stoppable : [] };
}

const PetSurface: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const binding = usePetBinding();
  const { capabilities } = useInstanceAuthorization();
  const inbox = useWorkbenchInbox();
  const { expanded, layout, awaiting: awaitingLayout, setPanel } = usePetPanel();
  const { draft, references: draftReferences, uncertain, sending, listenPending, setDraft, setUncertain } = usePetComposer(binding);
  const data = usePetSession(binding);
  const epoch = data.epoch;
  const canChat = capabilities.can_chat && data.canChat === true;
  useEffect(() => {
    if (canChat) petShell.normalizeMain(epoch);
  }, [canChat, epoch]);
  const { requests: vaultRequests } = usePendingVaultRequests(data.canChat === true ? binding ?? '' : '');
  const unreadCount = binding ? inbox.unreadBySession[binding] ?? 0 : 0;
  const state = derivePetState({
    session: data.session,
    turn: data.running ? { foreground: 'running', in_flight: true } : data.turn,
    pendingVaultRequests: vaultRequests.length,
    unreadCount,
  });
  const exchange = useMemo(() => latestExchange(data.messages, data.hasOlder, unreadCount), [data.messages, data.hasOlder, unreadCount]);
  const failure = useMemo(() => latestFailureNotice(data.messages), [data.messages]);
  const quickReplies = openQuickReplies(data.messages);
  const runningAgents = useConversationAgentSnapshot(capabilities.can_use_agents === true);
  const { rows: repliedAgents, pending: repliesPending } = usePetReplyActivities(capabilities.can_use_agents === true, epoch, inbox.inboxSessions ?? EMPTY_INBOX);
  const previewBySession = useMemo(
    () => Object.fromEntries((inbox.inboxSessions ?? []).map((row) => [row.session_id, row.preview_text ?? ''])),
    [inbox.inboxSessions],
  );
  const activitySnapshotReady = inbox.unreadLoaded === true && !inbox.loading && runningAgents.ok && !repliesPending;
  const visibleAgentRows = useMemo(() => (
    [...(runningAgents.ok ? runningAgents.agents : []), ...repliedAgents].filter((agent) => (
        agent.session_id
        && agent.session_id !== binding
        && agent.session_id !== 'ses_main'
        && agent.visibility === 'foreground'
        && agent.openable_in_chat
      ))
  ), [binding, runningAgents, repliedAgents]);
  const activitySessionIds = JSON.stringify([...new Set(visibleAgentRows.map((row) => row.session_id!))].sort());
  const cancellableIds = JSON.stringify(visibleAgentRows.filter((row) => (row.platform === 'avibe' || row.platform == null) && row.trigger_source === 'human' && row.state === 'active').map((row) => row.session_id).sort());
  const { requests: visibleVaultRequests, stoppable, ready: activityActionsReady } = useVisibleActivityActions(capabilities.can_use_agents === true, epoch, activitySessionIds, cancellableIds);
  const othersWorking = visibleAgentRows.some(isRunning);
  const busy = data.running || othersWorking;
  const [asleep, setAsleep] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const cardsExpanded = expanded && !detailOpen;
  const openCards = useCallback(() => {
    setDetailOpen(false);
    void setPanel(true);
  }, [setDetailOpen, setPanel]);
  // A Vibey click (§5.1) opens the main card immediately, closes it when it is
  // already open, and folds the other cards as part of opening.
  const toggleVibey = () => {
    if (historyOpen) { setDetailOpen(false); void setPanel(false); return; }
    if (!binding || resolvingMain) return;
    openHistory();
  };
  const [composing, setComposing] = useState(false);
  const [presentations, setPresentations] = useState<Record<string, CardPresentation>>({});
  const presentationNow = useRef<Record<string, CardPresentation>>({});
  const [generations, setGenerations] = useState<Record<string, number>>({});
  const generationNow = useRef<Record<string, number>>({});
  const activityPresence = useRef(new Map<string, boolean>());
  const stops = useMemo(() => Object.fromEntries(Object.entries(presentations)
    .filter(([, value]) => value.phase).map(([key, value]) => [key, value.phase!])), [presentations]);
  const leaving = useMemo(() => Object.keys(presentations).filter((key) => presentations[key].leaving), [presentations]);
  const retainedCards = useMemo(() => Object.fromEntries(Object.entries(presentations)
    .filter(([, value]) => value.retained).map(([key, value]) => [key, value.retained!])), [presentations]);
  const dismissed = usePetDismissed();
  const [optimisticTitle, setOptimisticTitle] = useState('');
  const reducedMotion = useReducedMotion();
  const [orderedIds, setOrderedIds] = useState<string[]>([]);
  const stopTimers = useRef<Record<string, number>>({});
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const avatarRef = useRef<HTMLButtonElement>(null);
  // Pair native mousedown click counts with time for webviews that always report 1.
  // A normal pointer click is handled on its pointer-up; this ref only decides
  // whether that click is also the second half of a double-click.
  const pressRef = useRef<{ held: boolean; dragged: boolean; clicks: number; double: boolean; x: number; y: number; timer: number } | null>(null);
  const lastClickAtRef = useRef(0);
  const composerBeforeClickRef = useRef(false);
  const doubleOpenedRef = useRef(false);
  const allowDoubleEventRef = useRef(false);
  const dismissTimers = useRef<Record<string, number>>({});
  const updatePresentation = useCallback((key: string, patch: CardPresentation | null) => {
    const next = { ...presentationNow.current };
    if (patch) next[key] = { ...next[key], ...patch };
    else delete next[key];
    presentationNow.current = next;
    setPresentations(next);
  }, []);
  const retireCard = useCallback((key: string) => {
    window.clearTimeout(stopTimers.current[key]);
    window.clearTimeout(dismissTimers.current[key]);
    delete stopTimers.current[key];
    delete dismissTimers.current[key];
    updatePresentation(key, null);
  }, [updatePresentation]);
  const isCurrentCard = useCallback((card: PetCard) => epoch.isCurrent()
    && card.id === `${card.sessionId}:${generationNow.current[card.sessionId] ?? 0}`, [epoch]);
  const advanceTurn = useCallback((sessionId: string) => {
    if (!epoch.isCurrent()) return;
    const previous = generationNow.current[sessionId] ?? 0;
    generationNow.current = { ...generationNow.current, [sessionId]: previous + 1 };
    retireCard(`${sessionId}:${previous}`);
    setGenerations(generationNow.current);
    petShell.forgetDismissed(sessionId);
  }, [epoch, retireCard]);
  const previousBinding = useRef(binding);
  useLayoutEffect(() => {
    setDetailOpen(false);
    // Authorization changes remove authorized presentation, not the user's text.
    if (previousBinding.current !== binding) {
      setComposing(false);
      setOptimisticTitle('');
      previousBinding.current = binding;
    }
    presentationNow.current = {};
    setPresentations({});
    generationNow.current = {};
    setGenerations({});
    activityPresence.current.clear();
    setOrderedIds([]);
    return () => {
      Object.values(stopTimers.current).forEach(window.clearTimeout);
      Object.values(dismissTimers.current).forEach(window.clearTimeout);
      if (pressRef.current) window.clearTimeout(pressRef.current.timer);
      stopTimers.current = {};
      dismissTimers.current = {};
      pressRef.current = null;
      lastClickAtRef.current = 0;
      doubleOpenedRef.current = false;
      allowDoubleEventRef.current = false;
    };
  }, [binding, epoch]);
  useEffect(() => {
    if (expanded) return;
    setDetailOpen(false);
  }, [expanded]);
  // The shell reports hover over Vibey and the dock: macOS gives a webview no
  // hover while its window is not key, and the pet never takes focus itself.
  const [hovered, setHovered] = useState(false);
  const [hoverSuppressed, setHoverSuppressed] = useState(false);
  const shellHoveredRef = useRef(false);
  const hoverSuppressedRef = useRef(false);
  const handlePetHover = useCallback((next: boolean) => {
    shellHoveredRef.current = next;
    if (next && hoverSuppressedRef.current) {
      setHovered(false);
      return;
    }
    if (!next) hoverSuppressedRef.current = false;
    // Keep CSS hover suppressed until a fresh enter, even if WebKit's :hover is stale.
    if (next) setHoverSuppressed(false);
    setHovered(next);
  }, []);
  useEffect(() => onPetHover(handlePetHover), [handlePetHover]);
  useEffect(() => {
    if (exchange.user?.text?.trim() === optimisticTitle) setOptimisticTitle('');
  }, [exchange.user, optimisticTitle]);

  const liveCards = useMemo(() => {
    const bySession = new Map<string, RunningAgent>();
    for (const agent of visibleAgentRows) {
      if (!agent.session_id) continue;
      const previous = bySession.get(agent.session_id);
      if (!previous || (agent.state === 'active' && previous.state !== 'active')) bySession.set(agent.session_id, agent);
    }
    return [...bySession.values()].map((agent) => agentCard(
      agent,
      inbox.unreadBySession,
      previewBySession,
      visibleVaultRequests,
      stoppable,
      t('pet.untitledTask'),
      (key, options) => String(t(key as never, options as never)),
    ))
    .filter((card): card is PetCard => Boolean(card && (card.running || card.hasUnread || card.needsInput)))
    .map((card) => ({ ...card, id: `${card.sessionId}:${generations[card.sessionId] ?? 0}` }));
  }, [
    inbox.unreadBySession,
    previewBySession,
    t,
    visibleAgentRows,
    visibleVaultRequests,
    stoppable,
    generations,
  ]);
  // A healthy disappearance followed by reappearance is also a new local turn,
  // including when its turn.start event was missed during a connection gap.
  useLayoutEffect(() => {
    if (!activitySnapshotReady || !activityActionsReady || !epoch.isCurrent()) return;
    const present = new Set(liveCards.map((card) => card.sessionId));
    for (const id of present) {
      if (activityPresence.current.get(id) === false) advanceTurn(id);
      activityPresence.current.set(id, true);
    }
    for (const id of activityPresence.current.keys()) {
      if (!present.has(id)) activityPresence.current.set(id, false);
    }
  }, [liveCards, activitySnapshotReady, activityActionsReady, advanceTurn, epoch]);
  const activities = useMemo(() => [
    ...liveCards.map((card) => retainedCards[card.id] ?? card),
    ...Object.values(retainedCards).filter((card) => !liveCards.some((live) => live.id === card.id)),
  ], [liveCards, retainedCards]);
  useEffect(() => {
    setOrderedIds((current) => {
      const live = new Set(activities.map((card) => card.id));
      const priority = (card: PetCard) => Number(card.needsInput) * 2 + Number(card.running);
      const fresh = activities.filter((card) => !current.includes(card.id)).sort((a, b) => priority(b) - priority(a)).map((card) => card.id);
      const byId = new Map(activities.map((card) => [card.id, card]));
      const next = cardsExpanded && current.length > 0
        ? [...current.filter((id) => live.has(id)), ...fresh].sort((a, b) => priority(byId.get(b)!) - priority(byId.get(a)!))
        : [...activities].sort((a, b) => priority(b) - priority(a)).map((card) => card.id);
      return next.length === current.length && next.every((id, i) => id === current[i]) ? current : next;
    });
  }, [activities, cardsExpanded]);
  useEffect(() => {
    if (!activitySnapshotReady) return;
    const liveIds = new Set([...visibleAgentRows.map((row) => row.session_id), ...Object.keys(inbox.unreadBySession).filter((id) => inbox.unreadBySession[id] > 0)]);
    for (const id of dismissed) if (!liveIds.has(id)) petShell.forgetDismissed(id);
  }, [visibleAgentRows, inbox.unreadBySession, dismissed, activitySnapshotReady]);

  const sortedActivities = useMemo(() => {
    const byId = new Map(activities.map((card) => [card.id, card]));
    const ordered = orderedIds.map((id) => byId.get(id)).filter((card): card is PetCard => Boolean(card));
    const fresh = activities.filter((card) => !orderedIds.includes(card.id));
    return [...fresh, ...ordered];
  }, [activities, orderedIds]);
  const visibleCards = useMemo(
    () => sortedActivities.filter((card) => !dismissed.includes(card.sessionId) && stops[card.id] !== 'exited'),
    [dismissed, sortedActivities, stops],
  );
  // A result or authorization arriving after a stopped turn still deserves
  // a card. This releases only that key; new turns use a new key altogether.
  useEffect(() => {
    if (!runningAgents.ok || !activityActionsReady) return;
    for (const [key, presentation] of Object.entries(presentations)) {
      const card = liveCards.find((item) => item.id === key);
      if (presentation.phase === 'exited' && (!card?.running || card.hasUnread || card.needsInput)) retireCard(key);
    }
  }, [presentations, liveCards, runningAgents.ok, activityActionsReady, retireCard]);
  useEffect(() => api.connectWorkbenchEvents({
    onTurnStart: ({ session_id }) => advanceTurn(session_id),
  }), [api, advanceTurn]);
  const cardNeedsInput = liveCards.some((card) => card.needsInput);
  const cardUnread = visibleCards.reduce((total, card) => total + (inbox.unreadBySession[card.sessionId] ?? 0), 0);
  useEffect(() => {
    setAsleep(false);
    if (state !== 'idle' || busy || cardNeedsInput || cardUnread > 0) return;
    const timer = window.setTimeout(() => { if (epoch.isCurrent()) setAsleep(true); }, 5 * 60 * 1000);
    return () => window.clearTimeout(timer);
  }, [state, busy, epoch, cardNeedsInput, cardUnread]);
  const pose: PetPose = cardNeedsInput ? 'needs_input'
    : state === 'idle' && cardUnread > 0 ? 'ready'
      : derivePetPose(state, { othersWorking: liveCards.some((card) => card.running), asleep });

  const closeComposer = useCallback(() => {
    setComposing(false);
    setDraft('');
  }, [setComposing, setDraft]);
  // An open history stays open: the input takes its place below it (§5.2).
  const openComposer = useCallback((text = '', selected?: SessionReference[]) => {
    if (!epoch.isCurrent()) return;
    setDraft(text, selected);
    setComposing(true);
    window.setTimeout(() => { if (epoch.isCurrent()) inputRef.current?.focus(); }, 0);
  }, [epoch, setComposing, setDraft]);

  const send = useCallback(async (text: string, metadata?: Record<string, unknown>) => {
    if (!epoch.isCurrent() || !text.trim() || !binding || !canChat || data.session?.id !== binding) return false;
    if (petComposer.isSending() || petComposer.isUncertain(binding)) return false;
    petComposer.setSending(true);
    try {
      const references = draftReferences.filter((reference) => text.includes(referenceToMarker(reference)));
      const row = await api.sendSessionMessage(binding, {
        text: text.trim(),
        ...(references.length ? { content: { references } } : {}),
        ...(metadata ? { metadata } : {}),
      });
      if (epoch.isCurrent()) {
        data.noteSent(row && typeof row === 'object' && 'id' in row ? row : null);
        setOptimisticTitle(text.trim());
      }
      return epoch.isCurrent();
    } catch (error) {
      if (!sendRefused(error)) {
        // Admission uncertainty belongs to the document's send ledger, even
        // after a recheck/remount. It must not permit another duplicate send.
        setUncertain(binding, true);
        if (epoch.isCurrent()) {
          setComposing(false);
          setDetailOpen(true);
          void setPanel(true);
          setOptimisticTitle(text.trim());
        }
      }
      return false;
    } finally {
      // Release this request's document-wide lock, not a new surface's state.
      petComposer.setSending(false);
    }
  }, [api, binding, canChat, data, draftReferences, epoch, setComposing, setDetailOpen, setPanel, setUncertain]);
  const submit = async () => {
    const submitted = draft;
    if (!(await send(submitted)) || !epoch.isCurrent()) return;
    if (petComposer.clearDraftIf(submitted)) setComposing(false);
  };
  const choose = (message: WorkbenchMessage, choice: string) => send(choice, { quick_reply_for: message.id });
  const referenceCard = useCallback((card: PetCard) => {
    if (!canChat || uncertain) return;
    const marker = referenceToMarker({ kind: 'session', session_id: card.sessionId });
    const next = draft.includes(marker) ? draft : [draft.trim(), marker].filter(Boolean).join(' ');
    const selected = draftReferences.some((reference) => reference.session_id === card.sessionId)
      ? draftReferences : [...draftReferences, { kind: 'session' as const, session_id: card.sessionId, title: card.title }];
    openComposer(next, selected);
  }, [draft, draftReferences, openComposer, canChat, uncertain]);
  const draftMarkers = draftReferences.map(referenceToMarker);
  const inputDraft = draftMarkers.length ? draftMarkers.reduce((text, marker) => text.replaceAll(marker, ''), draft).trimStart() : draft;

  const dismiss = useCallback((card: PetCard, manual = true) => {
    if (!isCurrentCard(card) || presentationNow.current[card.id]?.leaving) return;
    updatePresentation(card.id, { leaving: true, retained: card });
    const remove = () => {
      if (!isCurrentCard(card)) return;
      if (manual) petShell.addDismissed(card.sessionId);
      updatePresentation(card.id, { leaving: false, retained: undefined, ...(!manual ? { phase: 'exited' as const } : {}) });
    };
    if (reducedMotion) remove();
    else dismissTimers.current[card.id] = window.setTimeout(remove, DISMISS_EXIT_MS);
  }, [reducedMotion, isCurrentCard, updatePresentation]);

  const setStop = useCallback((card: PetCard) => {
    if (!isCurrentCard(card) || !card.canStop) return;
    const updatePhase = (phase?: StopPhase) => {
      if (isCurrentCard(card)) updatePresentation(card.id, { phase });
    };
    const phase = presentationNow.current[card.id]?.phase;
    if (!phase || phase === 'failed') {
      updatePhase('confirm');
      window.clearTimeout(stopTimers.current[card.id]);
      stopTimers.current[card.id] = window.setTimeout(() => updatePhase(), STOP_CONFIRM_MS);
      return;
    }
    if (phase !== 'confirm') return;
    window.clearTimeout(stopTimers.current[card.id]);
    updatePresentation(card.id, { phase: 'stopping', retained: card });
    void api.cancelSession(card.sessionId).then((result) => {
      if (!isCurrentCard(card)) return;
      if (!result.ok) throw new Error('stop failed');
      updatePhase('stopped');
      stopTimers.current[card.id] = window.setTimeout(() => dismiss(card, false), STOP_LINGER_MS);
    }).catch(() => {
      if (!isCurrentCard(card)) return;
      updatePresentation(card.id, { phase: 'failed', retained: undefined });
    });
  }, [api, dismiss, isCurrentCard, updatePresentation]);

  const hotkeyHeld = useRef(false);
  const resetVoiceShortcut = useCallback(() => {
    hotkeyHeld.current = false;
    void petBridge.resetVoiceShortcut().catch(() => undefined);
  }, []);

  const voice = usePetVoice({
    enabled: canChat && Boolean(binding) && !uncertain,
    epoch,
    draft,
    onOpenComposer: openComposer,
    onCaptureEnd: resetVoiceShortcut,
  });

  const clearVoiceError = voice.clearError;
  useEffect(() => { if (!composing) clearVoiceError(); }, [composing, clearVoiceError]);

  const onSummon = useCallback(() => {
    const intent = petShell.takeSummon();
    if (!intent) return;
    if (intent === 'release') {
      hotkeyHeld.current = false;
      voice.stop(true);
      if (!voice.busy && !composing) {
        void setPanel(true);
        petComposer.requestListen();
      }
      return;
    }
    if (intent === 'show') openCards();
    else void setPanel(true);
    if (intent === 'listen') {
      if (voice.finishing) { resetVoiceShortcut(); return; }
      hotkeyHeld.current = true;
      if (!binding || !canChat || uncertain) resetVoiceShortcut();
      petComposer.requestListen();
    }
  }, [setPanel, openCards, voice.stop, voice.busy, voice.finishing, composing, binding, canChat, uncertain, resetVoiceShortcut]);
  useOnSummon(onSummon);
  useEffect(() => {
    if (!listenPending || !binding || !canChat || !expanded) return;
    petComposer.takeListen();
    if (uncertain) { resetVoiceShortcut(); setDetailOpen(true); return; }
    if (hotkeyHeld.current && voice.available) voice.start();
    else { resetVoiceShortcut(); openComposer(draft); }
  }, [listenPending, binding, canChat, expanded, voice.available, voice.start, openComposer, draft, uncertain, resetVoiceShortcut]);
  const dockActiveRef = useRef(false);
  useLayoutEffect(() => { dockActiveRef.current = voice.busy || composing; }, [voice.busy, composing]);
  const suppressDock = useCallback((force = false) => {
    if (!force && dockActiveRef.current) return;
    // The shell emits transitions only; an already-outside pointer can enter next.
    hoverSuppressedRef.current = shellHoveredRef.current;
    setHoverSuppressed(true);
    setHovered(false);
    avatarRef.current?.blur();
  }, []);
  useEffect(() => {
    const onBlur = () => {
      suppressDock(doubleOpenedRef.current && !composerBeforeClickRef.current && !draft.trim() && !voice.busy);
      if (!voice.busy) resetVoiceShortcut();
      // Opening Avibe must preserve the history/card list through its key change.
      if (!doubleOpenedRef.current && !voice.busy && !draft.trim()) void setPanel(false);
    };
    const onFocus = () => { doubleOpenedRef.current = false; };
    window.addEventListener('blur', onBlur);
    window.addEventListener('focus', onFocus);
    return () => {
      window.removeEventListener('blur', onBlur);
      window.removeEventListener('focus', onFocus);
    };
  }, [draft, setPanel, suppressDock, voice.busy, resetVoiceShortcut]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // Escape also dismisses an IME candidate list; that one is not ours.
      if (event.key !== 'Escape' || isComposingKey(event)) return;
      if (!voice.busy) resetVoiceShortcut();
      // A history opened above the input closes before the input does.
      if (detailOpen && (voice.busy || composing)) { setDetailOpen(false); void setPanel(false); }
      else if (voice.busy) voice.cancel();
      else if (composing) closeComposer();
      else if (detailOpen) { setDetailOpen(false); void setPanel(false); }
      else void setPanel(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [closeComposer, composing, detailOpen, setPanel, voice, resetVoiceShortcut]);

  useEffect(() => {
    const node = inputRef.current;
    if (!node) return;
    node.style.height = 'auto';
    node.style.height = `${Math.min(node.scrollHeight, 96)}px`;
  }, [draft, composing]);

  const rootRef = useRef<HTMLDivElement>(null);
  useHitRegions(rootRef, epoch);
  // The input (or marquee) below an open history: its height leaves the
  // history less room in the fixed window.
  const [inputHeight, setInputHeight] = useState(0);
  const inputAreaRef = useCallback((node: HTMLDivElement | null) => {
    if (!node) return undefined;
    const measure = () => setInputHeight(node.offsetHeight);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node);
    return () => observer?.disconnect();
  }, []);
  const collapsedRef = useRef<HTMLDivElement>(null);
  const [collapsedHeight, setCollapsedHeight] = useState(0);
  // One card is always drawn in its full form (spec §3.3): there is no stack to
  // open, so Vibey's panel toggle never changes it, and it is measured like the
  // collapsed stack so the history beside it is sized for its real height.
  const singleCard = visibleCards.length === 1;
  const cardsFull = cardsExpanded || singleCard;
  const cardsListed = cardsExpanded && !singleCard;
  useLayoutEffect(() => {
    const node = collapsedRef.current;
    if (!node || cardsListed) return;
    // The entrance scales this wrapper; layout height must include its spacing
    // without capturing that transient transform.
    const measure = () => setCollapsedHeight(node.offsetHeight);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node);
    return () => observer?.disconnect();
  }, [cardsListed, visibleCards.length]);

  const queued = (data.turn?.pending_input_count ?? 0) > 0;
  const resolvingMain = data.canChat === null;
  const historyOpen = detailOpen && expanded;
  // What the bubble would draw: any exchange, or a state worth a word.
  const hasBubbleContent = Boolean(
    binding && !resolvingMain
    && (historyOpen || data.running || optimisticTitle || exchange.user || exchange.results.length > 0 || failure || uncertain
      || queued || vaultRequests.length > 0 || quickReplies),
  );
  // Whether it is drawn (spec §4.2): while the main session is active — Vibey
  // is working, a send is pending, Vibey needs the user, or the history is
  // open — and then for a short on-screen linger of a fresh reply. Unread
  // replies only light Vibey's badge. Every input is the current epoch's own
  // state, so nothing carries a bubble over from a previous session.
  const needsYou = Boolean((!data.running && failure) || uncertain || queued || vaultRequests.length > 0);
  // A history the user asked for but the shell has not confirmed yet holds the
  // bubble too: it neither folds nor spends the linger while it waits.
  const historyPending = detailOpen && !expanded;
  const held = Boolean(data.running || optimisticTitle || needsYou || historyOpen || historyPending);
  // A turn that ends while this epoch watches is what lingers: the moment the
  // server's turn state goes from running to done, derived during render from
  // the last value seen, so the render that ends the turn is the render that
  // starts the linger and the bubble never closes in between. The server's
  // state alone: the local send grace running out is not a turn that ended.
  // A turn already over when the epoch first looks is history.
  // One thing lingers: the reply of a turn this epoch watched end, pinned by
  // its id. Everything about the linger is derived from that reply during
  // render — whether there is one, what the summary shows, whether it was seen.
  //
  // `endedFor` is the latest input when the server's turn state was last seen
  // going from running to done (the local send grace is not a turn ending).
  // That turn's reply is the newest non-empty agent result after that input
  // that is not a background run's detached completion (an exchange can hold
  // an earlier answer from before this turn ran). Nothing before the input
  // counts, a promoted answer to an earlier input (#2815) included: it keeps
  // the badge but does not linger. A stopped turn ends with an empty result
  // and has none; a reply that lands after its turn end lingers then.
  // `seen` holds the replies shown while the history was open (expansion
  // confirmed by the shell); those do not linger again.
  const inputId = exchange.user?.id ?? '';
  const [turnWatch, setTurnWatch] = useState(() => ({ epoch, running: data.serverRunning, endedFor: null as string | null, seen: NO_REPLIES }));
  let watched = turnWatch;
  if (watched.epoch !== epoch) watched = { epoch, running: data.serverRunning, endedFor: null, seen: NO_REPLIES };
  else if (watched.running !== data.serverRunning) {
    watched = { ...watched, running: data.serverRunning, endedFor: data.serverRunning ? watched.endedFor : inputId };
  }
  const isTurnReply = (row: WorkbenchMessage) => row.author === 'agent' && row.type === 'result'
    && !isDetachedCompletionMessage(row) && Boolean(row.text?.trim());
  const inputAt = exchange.user ? data.messages.findIndex((row) => row.id === exchange.user!.id) : -1;
  const turnReply = watched.endedFor === inputId
    ? data.messages.slice(inputAt + 1).reverse().find(isTurnReply)
    : null;
  if (historyOpen && turnReply && !watched.seen.has(turnReply.id)) {
    watched = { ...watched, seen: new Set([...watched.seen, turnReply.id]) };
  }
  if (watched !== turnWatch) setTurnWatch(watched);
  const lingerReply = turnReply && !watched.seen.has(turnReply.id) ? turnReply : null;
  // The input or the dictation marquee holds the bottom of the speech slot.
  const inputShown = voice.busy || (composing && canChat && !uncertain);
  // The bubble is drawn: the speech slot shows it, alone or, while its history
  // is open or asked for, above the input or the marquee (§5.2). Every input
  // to the linger (time, pointer) means something only while this holds.
  const noReplyDetail = historyOpen
    && !data.running
    && !failure
    && exchange.results.length === 0
    && !uncertain
    && !queued
    && vaultRequests.length === 0
    && !quickReplies;
  const bubbleDrawn = hasBubbleContent && Boolean(binding) && (!inputShown || detailOpen) && !noReplyDetail;
  // The reply is on screen: the bubble is drawn and its summary row is the
  // reply (not "thinking" while the turn still runs, nor a failure notice).
  // Only then does the linger spend its time.
  const replyOnScreen = bubbleDrawn && !data.running && !failure;
  const documentVisible = useDocumentVisible();
  const [pointerOverBubble, setPointerOverBubble] = useState(false);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  // Focus inside a bubble that is replaced (by the input, the dictation
  // marquee, or a loading notice) moves to Vibey rather than to nowhere, or to
  // the input when the bubble closes above it. The node is checked as it
  // detaches, while it still holds the focus.
  const attachBubble = useCallback((node: HTMLDivElement | null) => {
    bubbleRef.current = node;
    if (!node) return undefined;
    return () => {
      if (bubbleRef.current === node) bubbleRef.current = null;
      if (node.contains(document.activeElement)) (inputRef.current ?? avatarRef.current)?.focus();
    };
  }, []);
  const { lingeringId, keepAtLeast } = useReplyLinger({
    scope: epoch,
    candidateId: lingerReply?.id ?? null,
    counting: documentVisible && replyOnScreen && !pointerOverBubble && !historyPending && !historyOpen && !inputShown,
    keyboardHeld: () => {
      const node = bubbleRef.current;
      const active = document.activeElement;
      return Boolean(node && active && node.contains(active) && active.matches(':focus-visible'));
    },
  });
  const replyNeedsUnreadDot = Boolean(
    turnReply
    && !watched.seen.has(turnReply.id)
    && lingeringId === null,
  );
  const [replyUnreadDotClearedFor, setReplyUnreadDotClearedFor] = useState<string | null>(null);
  const replyUnreadDot = Boolean(
    !historyOpen
    && turnReply
    && replyNeedsUnreadDot
    && replyUnreadDotClearedFor !== turnReply.id,
  );
  const showReplyBubble = hasBubbleContent && (held || lingeringId !== null);
  // A bubble that is not drawn, or is hidden, cannot be under the pointer, and
  // its leave event may never come (the input replaced it under a still
  // pointer); a stale reading would pause the reply for good.
  if ((!bubbleDrawn || !showReplyBubble) && pointerOverBubble) setPointerOverBubble(false);
  // Looking at the history is looking at the reply (`seen` above): once it
  // closes, the reply does not linger again.
  function openHistory() {
    if (turnReply) setReplyUnreadDotClearedFor(turnReply.id);
    setDetailOpen(true);
    if (canChat && !uncertain && !voice.busy) openComposer(draft);
    // A refused expansion leaves no history to wait for: drop the request so
    // the pending hold ends and the linger resumes.
    void setPanel(true).then(() => {
      if (!epoch.isCurrent() || petPanel.current()) return;
      setDetailOpen(false);
      if (!draft.trim()) setComposing(false);
    });
  }
  const toggleHistory = () => {
    if (detailOpen) { setDetailOpen(false); void setPanel(false); } else openHistory();
  };
  const openDoubleClick = () => {
    if (doubleOpenedRef.current) return;
    doubleOpenedRef.current = true;
    const preserveComposer = composerBeforeClickRef.current || Boolean(draft.trim()) || voice.busy;
    if (!preserveComposer) setComposing(false);
    if (turnReply) setReplyUnreadDotClearedFor(turnReply.id);
    setDetailOpen(false);
    void setPanel(false);
    const lastUnread = exchange.results.at(-1);
    if (binding && lastUnread?.read_at === null && exchange.unreadComplete) {
      void inbox.markRead(binding, lastUnread.id);
    }
    if (!binding) {
      suppressDock(!preserveComposer);
      return;
    }
    void petBridge.open(sessionLink(binding)).then(() => {
      if (epoch.isCurrent()) suppressDock(!preserveComposer);
    }, () => undefined);
  };
  useLayoutEffect(() => {
    // Focus inside a bubble that folds away (still mounted) moves to Vibey too.
    if (showReplyBubble) return;
    const node = bubbleRef.current;
    if (node && document.activeElement && node.contains(document.activeElement)) avatarRef.current?.focus();
  }, [showReplyBubble]);

  const avatar = (
    <div className={clsx(
      'pet-mini-avatar-wrap',
      voice.listening && 'is-listening',
      hovered && 'is-hovered',
      hoverSuppressed && 'is-hover-suppressed',
    )} data-pet-hover>
      {voice.listening && <span className="pet-mini-listen-ring" aria-hidden />}
      <button
        ref={avatarRef}
        type="button"
        className="pet-mini-avatar-button"
        // A click opens or closes Vibey's recent turns (§5.1).
        aria-label={t('pet.recentTurns')}
        aria-expanded={historyOpen}
        data-pet-hit
        onClick={(event) => {
          if (event.detail !== 0) return;
          doubleOpenedRef.current = false;
          allowDoubleEventRef.current = false;
          toggleVibey();
        }}
        onDoubleClick={() => {
          if (allowDoubleEventRef.current) openDoubleClick();
        }}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          const target = event.currentTarget;
          const pointerId = event.pointerId;
          doubleOpenedRef.current = false;
          allowDoubleEventRef.current = false;
          const now = performance.now();
          const isSecondClick = event.detail >= 2
            || (lastClickAtRef.current > 0 && now - lastClickAtRef.current <= DOUBLE_CLICK_DETECTION_MS);
          if (!isSecondClick) composerBeforeClickRef.current = composing || voice.busy || Boolean(draft.trim());
          pressRef.current = {
            held: false,
            dragged: false,
            clicks: event.detail || 1,
            double: isSecondClick,
            x: event.clientX,
            y: event.clientY,
            timer: window.setTimeout(() => {
              if (!epoch.isCurrent()) return;
              const press = pressRef.current;
              if (!press) return;
              press.held = true;
              lastClickAtRef.current = 0;
              doubleOpenedRef.current = false;
              allowDoubleEventRef.current = false;
              target.setPointerCapture?.(pointerId);
              if (!canChat || !binding) { void setPanel(true); return; }
              if (uncertain) { void setPanel(true); setDetailOpen(true); return; }
              if (voice.available) voice.start();
              else openComposer(draft);
            }, HOLD_MS),
          };
        }}
        onMouseDown={(event) => {
          const press = pressRef.current;
          if (press) {
            press.clicks = event.detail;
            if (!press.double && event.detail >= 2) press.double = true;
          }
          // The input keeps its focus and caret: a Vibey click is not leaving it.
          if (inputRef.current && document.activeElement === inputRef.current) event.preventDefault();
        }}
        onPointerMove={(event) => {
          const press = pressRef.current;
          if (!press || press.held || press.dragged || event.buttons !== 1) return;
          if (Math.hypot(event.clientX - press.x, event.clientY - press.y) < 4) return;
          press.dragged = true;
          window.clearTimeout(press.timer);
          lastClickAtRef.current = 0;
          doubleOpenedRef.current = false;
          allowDoubleEventRef.current = false;
          // The drop's layout arrives as the shell's snapshot (petShell).
          void petBridge.drag().catch(() => undefined);
        }}
        onPointerCancel={() => {
          const press = pressRef.current;
          pressRef.current = null;
          if (press) window.clearTimeout(press.timer);
          lastClickAtRef.current = 0;
          doubleOpenedRef.current = false;
          allowDoubleEventRef.current = false;
          if (press?.held) voice.stop(true);
        }}
        onPointerUp={() => {
          const press = pressRef.current;
          pressRef.current = null;
          if (!press) return;
          window.clearTimeout(press.timer);
          if (press.held) {
            voice.stop(true);
            doubleOpenedRef.current = false;
            allowDoubleEventRef.current = false;
          } else if (press.dragged || press.clicks > 2) {
            lastClickAtRef.current = 0;
            doubleOpenedRef.current = false;
            allowDoubleEventRef.current = false;
          } else if (press.double) {
            lastClickAtRef.current = 0;
            allowDoubleEventRef.current = true;
            toggleVibey();
            openDoubleClick();
          } else {
            lastClickAtRef.current = performance.now();
            toggleVibey();
          }
        }}
        onPointerLeave={() => {
          const press = pressRef.current;
          if (press && !press.held) {
            window.clearTimeout(press.timer);
            pressRef.current = null;
            lastClickAtRef.current = 0;
            doubleOpenedRef.current = false;
            allowDoubleEventRef.current = false;
          }
        }}
        onContextMenu={(event) => {
          event.preventDefault();
          void petBridge.contextMenu().catch(() => undefined);
        }}
      >
        <PetAvatar pose={voice.listening ? 'listening' : pose} badge={unreadCount + cardUnread} />
        {replyUnreadDot && unreadCount + cardUnread === 0 && (
          <span className="pet-mini-unread-dot pet-mini-avatar-unread-dot" aria-label={t('pet.unreadBadge', { count: 1 })} />
        )}
      </button>
      <MiniDock
        enabled={canChat && Boolean(binding)}
        listening={voice.listening}
        finishing={voice.finishing}
        typing={composing}
        // Availability gates starting a capture; an active one always keeps its stop control.
        showMic={voice.available || voice.busy}
        onMic={() => (voice.listening ? voice.stop(true) : voice.start())}
        onKeyboard={() => {
          if (uncertain) { void setPanel(true); setDetailOpen(true); return; }
          voice.stop(true);
          if (composing) closeComposer(); else openComposer();
        }}
        t={(key) => String(t(key as never))}
      />
    </div>
  );

  return (
    <div ref={rootRef} className="pet-mini-stage" data-pet-root data-busy={busy} data-state={state} data-edge={layout.panel_edge} data-layout-pending={awaitingLayout || undefined}>
      <div className={clsx('pet-mini-column', voice.busy && 'is-voice-active')} style={{ '--pet-collapsed-height': `${collapsedHeight}px`, '--pet-input-space': bubbleDrawn && inputShown ? `calc(${inputHeight}px + var(--pet-input-gap))` : '0px' } as CSSProperties}>
        <div className={clsx('pet-mini-cards', voice.busy && 'is-hidden')} inert={voice.busy || undefined} aria-label={cardsExpanded ? t('pet.panel') : undefined} style={{ height: cardsListed ? (visibleCards.length > 0 ? STACK_HEIGHT : 'auto') : collapsedHeight }}>
          <div ref={collapsedRef} key={singleCard ? 'single' : cardsExpanded ? 'expanded' : 'collapsed'} className="pet-mini-swap is-switching" onAnimationEnd={(event) => { if (event.target === event.currentTarget) event.currentTarget.classList.remove('is-switching'); }}>
          {visibleCards.length > 0 && (
            cardsFull ? (
              <ExpandedCards
                natural={singleCard}
                cards={visibleCards}
                leaving={leaving}
                onReference={referenceCard}
                onDismiss={dismiss}
                stops={stops}
                onStop={setStop}
                t={(key, options) => String(t(key as never, options as never))}
              />
            ) : (
              <CollapsedCards cards={visibleCards} edge={layout.panel_edge} onOpen={openCards} t={(key, options) => String(t(key as never, options as never))} />
            )
          )}
          </div>
        </div>
        <SpeechSlot open={voice.busy || composing || showReplyBubble || resolvingMain || !binding}>
          {bubbleDrawn && showReplyBubble && binding ? (
            // Still drawn while the slot closes, so it fades out rather than vanishing.
            <div
              // Its own identity, so a notice drawn in the same place replaces
              // it (and runs the focus handoff) instead of reusing this node.
              key="vibey-bubble"
              ref={attachBubble}
              className={clsx(inputShown && 'pet-mini-bubble-over-input')}
              onPointerEnter={() => setPointerOverBubble(true)}
              onPointerLeave={() => {
                // Leaving gives the reader a moment before it goes.
                keepAtLeast(REPLY_LINGER_RESUME_MS);
                setPointerOverBubble(false);
              }}
            >
            <MainReplyBubble
              open={historyOpen}
              onToggle={toggleHistory}
              pinnedReply={lingeringId !== null ? lingerReply : null}
              epoch={epoch}
              voiceNeedsCloud={voice.cloudRequired}
              canChat={canChat}
              binding={binding}
              exchange={exchange}
              failure={data.running ? null : failure}
              running={data.running}
              queued={(data.turn?.pending_input_count ?? 0) > 0}
              markRead={inbox.markRead}
              onRead={data.refreshTail}
              vaultRequestIds={vaultRequests.map((request) => request.id)}
              quickReplies={quickReplies}
              onChoose={choose}
              uncertain={uncertain}
              reducedMotion={reducedMotion}
              onInspect={() => { void petBridge.open(sessionLink(binding)).then(() => { if (epoch.isCurrent()) setUncertain(binding, false); }).catch(() => undefined); }}
            />
            </div>
          ) : !inputShown && (resolvingMain || !binding) ? (
            <div className="pet-mini-reply-bubble pet-mini-detail-inner" data-pet-hit role="status">
              <p className="pet-mini-detail-muted">{t(data.canChat === null ? 'common.loading' : 'pet.vibeyUnavailable')}</p>
              <Button variant="link" size="sm" className="px-0" onClick={() => void petBridge.open('avibe://settings')}>{t('pet.openInAvibe')}</Button>
            </div>
          ) : null}
          {inputShown && <div ref={inputAreaRef}>{voice.busy ? <Marquee text={voice.finishing ? '' : voice.preview} listeningLabel={t(voice.finishing ? 'pet.voice.finishing' : 'pet.voice.listening')} /> : (
          <>
          {voice.error && <p className="pet-mini-input-note" role="status">{t(`pet.voice.${voice.error}`)}</p>}
          <form className="pet-mini-input" data-pet-hit onSubmit={(event) => { event.preventDefault(); void submit(); }}>
            {draftReferences.length > 0 && <div className="pet-mini-references">
              {draftReferences.map((reference) => <span key={reference.session_id} className="pet-mini-reference-chip">
                <span title={reference.title ?? reference.session_id}># {reference.title ?? reference.session_id}</span>
                <button type="button" aria-label={t('pet.removeReference', { title: reference.title ?? reference.session_id })} onClick={() => setDraft(draft.replaceAll(referenceToMarker(reference), '').trim())}><X size={12} /></button>
              </span>)}
            </div>}
            <div className="pet-mini-input-row">
            <textarea
              ref={inputRef}
              rows={1}
              value={inputDraft}
              onChange={(event) => setDraft([...draftMarkers, event.target.value].join(' '))}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !isComposingKey(event)) {
                  event.preventDefault();
                  void submit();
                }
              }}
              placeholder={t('pet.inputPlaceholder')}
              aria-label={t('pet.inputPlaceholder')}
            />
            {voice.available && <button type="button" className="pet-mini-input-button" onClick={() => voice.start()} aria-label={t('pet.voiceContinue')}><MicIcon /></button>}
            <button type="submit" className="pet-mini-send" disabled={!draft.trim() || sending || uncertain} aria-label={t('pet.send')}><SendIcon /></button>
            </div>
          </form>
          </>
          )}</div>}
        </SpeechSlot>
      </div>
      {avatar}
    </div>
  );
};


function useReducedMotion() {
  const [reduced, setReduced] = useState(() => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);
  useEffect(() => {
    const query = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    const update = () => setReduced(query?.matches ?? false);
    query?.addEventListener('change', update);
    return () => query?.removeEventListener('change', update);
  }, []);
  return reduced;
}

function useHitRegions(rootRef: React.RefObject<HTMLDivElement | null>, epoch?: PetSurfaceEpoch) {
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    let frame = 0;
    let movingUntil = 0;
    let last = '';
    let active = true;
    const report = () => {
      if (!active) return;
      if (epoch && !epoch.isCurrent()) {
        frame = 0;
        void petBridge.setHitRegions([]).catch(() => undefined);
        return;
      }
      const rects = Array.from(root.querySelectorAll<HTMLElement>('[data-pet-hit]')).flatMap((node) => {
        if (node.closest('[inert], .is-leaving')) return [];
        const style = getComputedStyle(node);
        if (style.visibility === 'hidden' || style.pointerEvents === 'none' || Number(style.opacity) === 0) return [];
        const rect = node.getBoundingClientRect();
        let left = Math.max(0, rect.left), top = Math.max(0, rect.top);
        let right = Math.min(innerWidth, rect.right), bottom = Math.min(innerHeight, rect.bottom);
        for (let parent = node.parentElement; parent && parent !== root; parent = parent.parentElement) {
          const parentStyle = getComputedStyle(parent);
          if (Number(parentStyle.opacity) === 0 || parentStyle.visibility === 'hidden') return [];
          if (/(hidden|auto|scroll|clip)/.test(parentStyle.overflowX + parentStyle.overflowY)) {
            const clip = parent.getBoundingClientRect();
            left = Math.max(left, clip.left); right = Math.min(right, clip.right);
            top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom);
          }
        }
        const hover = node.closest('[data-pet-hover]') !== null;
        return right > left && bottom > top ? [{ x: left, y: top, width: right - left, height: bottom - top, ...(hover ? { hover } : {}) }] : [];
      });
      const encoded = JSON.stringify(rects);
      if (encoded !== last) {
        last = encoded;
        void petBridge.setHitRegions(rects).catch(() => undefined);
      }
      frame = performance.now() < movingUntil ? requestAnimationFrame(report) : 0;
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(report); };
    const motion = () => { movingUntil = performance.now() + 550; schedule(); };
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
    const observe = () => { resize?.disconnect(); resize?.observe(root); root.querySelectorAll<HTMLElement>('[data-pet-hit]').forEach((node) => resize?.observe(node)); motion(); };
    const mutations = new MutationObserver(observe);
    mutations.observe(root, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'style', 'inert'] });
    // Any attribute of the stage itself can move or show what is drawn (the
    // layout's `data-edge`, `data-layout-pending`), without a resize. Reports
    // stay one per frame (`schedule`) and are sent only when they change.
    const stage = new MutationObserver(schedule);
    stage.observe(root, { attributes: true });
    root.addEventListener('scroll', schedule, true);
    root.addEventListener('transitionrun', motion);
    root.addEventListener('pointerover', motion);
    root.addEventListener('pointerout', motion);
    observe();
    return () => {
      active = false;
      cancelAnimationFrame(frame); resize?.disconnect(); mutations.disconnect(); stage.disconnect();
      root.removeEventListener('scroll', schedule, true);
      root.removeEventListener('transitionrun', motion);
      root.removeEventListener('pointerover', motion);
      root.removeEventListener('pointerout', motion);
      // Authorization rechecks can remove this surface while its native window
      // stays alive. Release its desktop input regions together with the DOM.
      void petBridge.setHitRegions([]).catch(() => undefined);
    };
  }, [rootRef, epoch]);
}

function usePetVoice({ enabled, epoch, draft, onOpenComposer, onCaptureEnd }: { enabled: boolean; epoch: PetSurfaceEpoch; draft: string; onOpenComposer: (text?: string) => void; onCaptureEnd: () => void }) {
  const [asr, setAsr] = useState<{ epoch: PetSurfaceEpoch; enabled: boolean; available: boolean; checked: boolean; maxFileBytes?: number }>({ epoch, enabled, available: false, checked: false });
  const [preview, setPreview] = useState('');
  const [listening, setListening] = useState(false);
  // Stop has been requested; capture and cleanup still run until `done`.
  const [finishing, setFinishing] = useState(false);
  // A failed capture or transcription falls back to text with this note.
  const [error, setError] = useState<VoiceFailure | null>(null);
  type Capture = { dictation: ShowPageVoiceDictation; ending: boolean; cancelled: boolean };
  const captureRef = useRef<Capture | null>(null);
  const supported = typeof navigator !== 'undefined' && Boolean(navigator.locks && navigator.mediaDevices?.getUserMedia);
  const asrForEpoch = asr.epoch === epoch && asr.enabled === enabled ? asr : { epoch, enabled, available: false, checked: false };
  const available = enabled && supported && asrForEpoch.available;
  // Read ASR status as soon as voice may be used. The pet window is created
  // unfocused, so this read does not wait for activation. Status is keyed to
  // this bind/epoch: a previous available result cannot start a capture on
  // the next one. Settings changes take effect on that next epoch.
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    void fetch('/api/asr/status', { credentials: 'same-origin' }).then((response) => response.ok ? response.json() : null).then((value) => {
      if (!alive || !epoch.isCurrent()) return;
      if (value === null) setAsr({ epoch, enabled, available: false, checked: false });
      else setAsr({ epoch, enabled, available: Boolean(value.available), checked: true, maxFileBytes: value.max_file_bytes });
    }).catch(() => { if (alive && epoch.isCurrent()) setAsr({ epoch, enabled, available: false, checked: false }); });
    return () => { alive = false; };
  }, [enabled, epoch]);
  const stop = useCallback((commit: boolean) => {
    const capture = captureRef.current;
    if (!capture) return;
    if (!capture.ending) onCaptureEnd();
    // A second commit is a no-op. Cancel must still abort an in-flight finish
    // (Escape, epoch change) so finishing cannot stick on the next surface.
    if (commit) {
      if (capture.ending) return;
      capture.ending = true;
      setListening(false);
      setFinishing(true);
      capture.dictation.finish();
      return;
    }
    if (capture.cancelled) return;
    capture.ending = true;
    capture.cancelled = true;
    captureRef.current = null;
    capture.dictation.abort();
    setListening(false);
    setFinishing(false);
    setPreview('');
  }, [onCaptureEnd]);
  const start = useCallback(() => {
    if (!epoch.isCurrent() || !available || captureRef.current) return;
    const before = draft;
    const capture: Capture = {
      dictation: new ShowPageVoiceDictation({ before, after: '', maxFileBytes: asrForEpoch.maxFileBytes, onPreview: (text) => { if (epoch.isCurrent()) setPreview(text); } }),
      ending: false,
      cancelled: false,
    };
    captureRef.current = capture;
    setListening(true);
    setFinishing(false);
    setError(null);
    const settle = () => {
      if (captureRef.current !== capture || !epoch.isCurrent()) return false;
      if (!capture.ending) onCaptureEnd();
      captureRef.current = null;
      setListening(false);
      setFinishing(false);
      setPreview('');
      return !capture.cancelled;
    };
    // Whichever of start/done rejects first settles; the other is a no-op.
    const fail = (reason: unknown) => {
      if (!settle()) return;
      if (reason instanceof VoiceTranscriptionError && reason.code === 'cancelled') return;
      setError(voiceFailure(reason));
      onOpenComposer(before);
    };
    void capture.dictation.done.then((text) => {
      if (!settle()) return;
      const next = text.trim();
      onOpenComposer(next ? [before.trim(), next].filter(Boolean).join(' ') : before);
    }).catch((reason: unknown) => fail(reason));
    void capture.dictation.start().then(() => {
      if (!epoch.isCurrent()) { capture.dictation.abort(); return; }
      // Release may precede microphone permission or pipeline startup.
      if (capture.ending && !capture.cancelled) capture.dictation.finish();
    }).catch((reason: unknown) => { fail(reason); capture.dictation.abort(); });
  }, [asrForEpoch.maxFileBytes, available, epoch, draft, onOpenComposer, onCaptureEnd]);
  const cancel = useCallback(() => stop(false), [stop]);
  const clearError = useCallback(() => setError(null), []);
  useEffect(() => cancel, [epoch, enabled, cancel]);
  useEffect(() => () => setError(null), [epoch, enabled]);
  return {
    available,
    cloudRequired: asrForEpoch.checked && !asrForEpoch.available,
    listening,
    finishing,
    busy: listening || finishing,
    preview,
    error,
    clearError,
    start,
    stop,
    cancel,
  };
}

type VoiceFailure = 'startFailed' | 'empty' | 'failed';

// Anything that is not a transcription error comes from getting the
// microphone itself (permission denied, no input device, capture setup).
function voiceFailure(reason: unknown): VoiceFailure {
  if (!(reason instanceof VoiceTranscriptionError)) return 'startFailed';
  return reason.code === 'empty' ? 'empty' : 'failed';
}

const SpeechSlot: React.FC<{ open: boolean; children: ReactNode }> = ({ open, children }) => (
  <div className={clsx('pet-mini-slot', open && 'is-open')} aria-hidden={!open} inert={!open ? true : undefined}>
    <div className="pet-mini-slot-inner"><div className="pet-mini-slot-body">{children}</div></div>
  </div>
);

const Marquee: React.FC<{ text: string; listeningLabel: string }> = ({ text, listeningLabel }) => {
  const boxRef = useRef<HTMLDivElement>(null);
  const lineRef = useRef<HTMLSpanElement>(null);
  const [shift, setShift] = useState(0);
  useEffect(() => {
    const box = boxRef.current;
    const line = lineRef.current;
    if (!box || !line) return;
    setShift(Math.max(0, line.scrollWidth - box.clientWidth));
  }, [text]);
  return (
    <div className="pet-mini-marquee" data-pet-hit>
      <span className="pet-mini-voice-dot" aria-hidden><span /></span>
      <div ref={boxRef} className={clsx('pet-mini-marquee-line', shift > 0 && 'is-overflowing')}>
        <span ref={lineRef} className="pet-mini-marquee-text" style={{ transform: `translateX(${-shift}px)` }} aria-live="polite">{text || <span className="pet-mini-muted">{listeningLabel}</span>}</span>
      </div>
    </div>
  );
};

const MiniDock: React.FC<{ enabled: boolean; listening: boolean; finishing?: boolean; typing: boolean; showMic: boolean; onMic: () => void; onKeyboard: () => void; t: (key: string) => string }> = ({ enabled, listening, finishing, typing, showMic, onMic, onKeyboard, t }) => (
  <div className={clsx('pet-mini-dock', (listening || finishing || typing) && 'is-active')} data-pet-hit>
    {showMic && <button type="button" className={clsx(listening && 'is-listening')} disabled={finishing} onClick={onMic} aria-pressed={listening} aria-label={listening ? t('pet.voice.stop') : t('pet.voice.open')}>{listening ? <StopIcon /> : <MicIcon />}</button>}
    {showMic && <span className="pet-mini-dock-divider" aria-hidden />}
    <button type="button" className={clsx(typing && 'is-typing')} disabled={!enabled} onClick={onKeyboard} aria-pressed={typing} aria-label={typing ? t('pet.keyboard.close') : t('pet.keyboard.open')}><KeyboardIcon /></button>
  </div>
);

// The layers peek out toward Vibey: below the front card, or above it when the
// column opens downward.
const CollapsedCards: React.FC<{ cards: PetCard[]; edge: PetLayout['panel_edge']; onOpen: () => void; t: (key: string, options?: Record<string, unknown>) => string }> = ({ cards, edge, onOpen, t }) => {
  const [front, ...remainingCards] = cards;
  if (!front) return null;
  const layers = remainingCards.slice(0, MAX_COLLAPSED_LAYERS);
  const count = cards.length;
  const displayCount = count > MAX_COLLAPSED_COUNT ? `${MAX_COLLAPSED_COUNT}+` : String(count);
  return (
    <button
      type="button"
      className="pet-mini-collapsed"
      style={edge === 'top' ? { paddingTop: layers.length * 9 } : { paddingBottom: layers.length * 9 }}
      onClick={onOpen}
      aria-label={t('pet.stack.expand', { count, displayCount })}
    >
      {layers.map((card, index) => {
        const inset = 18 + 6 + index * 3;
        const near = (index + 1) * 9, far = (layers.length - index - 1) * 9;
        return <span key={card.id} data-pet-hit className="pet-mini-layer" style={{ top: edge === 'top' ? far : near, bottom: edge === 'top' ? near : far, left: inset, right: inset, zIndex: layers.length - index }} aria-hidden />;
      })}
      <div className="pet-mini-front" style={{ zIndex: layers.length + 1 }}>
        <CardBody
          card={front}
          expanded={false}
          t={t}
          stackCount={count > 1 ? t('pet.stack.count', { count, displayCount }) : undefined}
        />
      </div>
    </button>
  );
};

/**
 * Scroll edge fades (spec §7.5): each edge fades only once there is content
 * past it. Recomputed on scroll and whenever the box or its content resizes.
 */
function useScrollEdgeFade(contentKey: unknown, profile: FadeProfile) {
  const ref = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ top: 0, bottom: 0 });
  const measure = useCallback(() => {
    const node = ref.current;
    if (!node) return;
    setEdges({ top: Math.min(profile.size, Math.max(0, node.scrollTop)), bottom: Math.min(profile.size, Math.max(0, node.scrollHeight - node.clientHeight - node.scrollTop)) });
  }, [profile.size]);
  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    for (const child of node.children) observer.observe(child);
    return () => observer.disconnect();
  }, [measure, contentKey]);
  // Each band is as tall as the content past that edge, up to the profile's
  // size, so there is no fade at the top before scrolling or at the end.
  const top = profile.ramp.map(([alpha, at]) => `rgba(0,0,0,${alpha}) ${edges.top * at}px`);
  const bottom = [...profile.ramp].reverse().map(([alpha, at]) => `rgba(0,0,0,${alpha}) calc(100% - ${edges.bottom * at}px)`);
  const mask = `linear-gradient(to bottom, ${['transparent 0', ...top, `#000 ${edges.top}px`, `#000 calc(100% - ${edges.bottom}px)`, ...bottom, 'transparent 100%'].join(', ')})`;
  return { ref, measure, style: { maskImage: mask, WebkitMaskImage: mask } satisfies CSSProperties };
}

/** The full card list. `natural` sizes it to its content instead of the 292px
 *  scroll box, for the lone card that is always drawn in full. */
const ExpandedCards: React.FC<{ natural?: boolean; cards: PetCard[]; leaving: string[]; onReference: (card: PetCard) => void; onDismiss: (card: PetCard) => void; stops: Record<string, StopPhase>; onStop: (card: PetCard) => void; t: (key: string, options?: Record<string, unknown>) => string }> = ({ natural = false, cards, leaving, onReference, onDismiss, stops, onStop, t }) => {
  const { ref: fadeRef, measure: measureFade, style: fadeStyle } = useScrollEdgeFade(cards.length, CARD_FADE);
  return (
    <div className={clsx('pet-mini-panel', natural && 'is-natural')}>
      <div ref={fadeRef} onScroll={measureFade} className="pet-mini-scroll" style={natural ? undefined : fadeStyle}>
        <ul className="pet-mini-list">
          {cards.map((card) => {
            const phase = stops[card.id];
            return (
              <li key={card.id} className={clsx('pet-mini-row', leaving.includes(card.id) && 'is-leaving')}>
                <div className="pet-mini-row-inner"><div className="pet-mini-row-body">
                  <div className="pet-mini-card-wrap" data-pet-hit>
                    <CardBody card={card} expanded t={t} phase={phase} onDismiss={() => onDismiss(card)} onStop={() => onStop(card)} onClick={() => onReference(card)} />
                  </div>
                </div></div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
};

const CardBody: React.FC<{ card: PetCard; expanded: boolean; t: (key: string, options?: Record<string, unknown>) => string; stackCount?: string; phase?: StopPhase; onDismiss?: () => void; onStop?: () => void; onClick?: () => void }> = ({ card, expanded, t, stackCount, phase, onDismiss, onStop, onClick }) => {
  const running = card.running && phase !== 'stopped' && phase !== 'stopping';
  const subtitle = phase === 'stopping' ? t('pet.stop.stopping') : phase === 'stopped' ? t('pet.stop.stopped') : phase === 'failed' ? t('pet.stop.failed') : card.status;
  const stopText = phase === 'confirm' ? t('pet.stop.confirmStop') : t('pet.stop.stop', { title: card.title });
  const Reference = expanded ? 'button' : 'span';
  const unread = card.hasUnread && !card.running && !card.needsInput;
  return (
    <article className={clsx('pet-mini-card', card.needsInput && 'is-needs-input')} title={card.title} data-session-id={card.sessionId} data-pet-hit>
      {expanded && <button type="button" className="pet-mini-dismiss" data-pet-hit onClick={onDismiss} aria-label={t('pet.dismiss')}><svg viewBox="0 0 16 16" aria-hidden><path d="m4 4 8 8M12 4 4 12" /></svg></button>}
      <Reference className="pet-mini-card-content pet-mini-card-reference" onClick={onClick} {...(expanded ? { type: 'button' as const, 'aria-label': t('pet.referenceSession', { title: card.title }) } : {})}>
        <div className="pet-mini-eyebrow">
          <span className="pet-mini-eyebrow-text" title={card.eyebrow}>{card.eyebrow}</span>
          {stackCount && <span className="pet-mini-stack-count">{stackCount}</span>}
        </div>
        <div className="pet-mini-title">{card.title}</div>
        <div className={clsx('pet-mini-subtitle', phase === 'stopped' && 'is-stopped')} title={subtitle}>
          {running && <span className="pet-mini-running-dot" aria-hidden />}
          {phase === 'stopped' && <span className="pet-mini-stopped-dot" aria-hidden />}
          {unread && <span className="pet-mini-unread-dot" aria-hidden />}
          {subtitle && <span>{subtitle}</span>}
        </div>
      </Reference>
      {expanded && (
        <div className="pet-mini-card-actions">
          {card.needsInput && card.vaultRequestIds[0] ? (
            <button type="button" className="pet-mini-authorize" onClick={() => void petBridge.open(vaultRequestLink(card.vaultRequestIds[0]))}><KeyRound className="size-3.5" />{t('pet.authorize')}</button>
          ) : card.canStop && phase !== 'stopped' ? (
            <button type="button" className={clsx('pet-mini-stop', phase === 'confirm' && 'is-confirm')} onClick={onStop} disabled={phase === 'stopping'} aria-label={stopText}>
              {phase === 'confirm' ? stopText : phase === 'stopping' ? <span className="pet-mini-spinner" aria-hidden /> : <StopIcon />}
            </button>
          ) : null}
          {card.canOpen && <PetOpenButton link={sessionLink(card.sessionId)} placement="start" />}
        </div>
      )}
    </article>
  );
};

type MainDetailProps = { epoch: PetSurfaceEpoch; voiceNeedsCloud: boolean; canChat: boolean; visible: boolean; binding: string; exchange: ReturnType<typeof latestExchange>; failure: WorkbenchMessage | null; running: boolean; queued: boolean; markRead: (sessionId: string, untilMessageId?: string) => Promise<boolean>; onRead: () => void; vaultRequestIds: string[]; quickReplies: ReturnType<typeof openQuickReplies>; onChoose: (message: WorkbenchMessage, choice: string) => Promise<boolean>; uncertain: boolean; onInspect: () => void };

const MessageContent: React.FC<{ message: WorkbenchMessage }> = ({ message }) => {
  const { t } = useTranslation();
  const attachments = readMessageAttachments(message.content);
  return (
    <>
      <Markdown
        content={message.text}
        references={(message.content as { references?: MentionReference[] })?.references}
        softBreaks
      />
      {attachments.length > 0 && (
        <div className="pet-mini-message-attachments">
          {attachments.map((attachment, index) => {
            if (!attachment.url) return null;
            return attachment.image ? (
              <ChatImage key={index} src={attachment.url} alt={attachment.name} width={attachment.width} height={attachment.height} />
            ) : (
              <FileCard key={index} href={attachment.url}>{attachment.name || t('pet.fileAttachment')}</FileCard>
            );
          })}
        </div>
      )}
    </>
  );
};

/**
 * Animates a box between two widths CSS cannot interpolate (its content's width
 * and the column's): from the width last drawn to the new one, then hands the
 * width back to CSS. Reduced motion switches at once.
 */
function useWidthMorph(key: unknown, reducedMotion: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const drawn = useRef<number | null>(null);
  const previous = useRef(key);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => { drawn.current = node.getBoundingClientRect().width; });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const changed = previous.current !== key;
    previous.current = key;
    const from = drawn.current;
    // Every width set here applies at once: only the last one is animated. A
    // width CSS can interpolate would otherwise start its own transition from
    // whatever was computed in between.
    const settle = (width: string) => {
      node.style.transition = 'none';
      node.style.width = width;
      void node.offsetWidth;
      node.style.transition = '';
    };
    // Whatever was moving lands at once when there is nothing to animate.
    settle('');
    if (!changed || reducedMotion) return undefined;
    const to = node.getBoundingClientRect().width;
    if (from === null || Math.abs(from - to) < 1) return undefined;
    settle(`${from}px`);
    node.style.width = `${to}px`;
    const release = (event: TransitionEvent) => {
      if (event.target === node && event.propertyName === 'width') settle('');
    };
    node.addEventListener('transitionend', release);
    node.addEventListener('transitioncancel', release);
    return () => {
      node.removeEventListener('transitionend', release);
      node.removeEventListener('transitioncancel', release);
    };
  }, [key, reducedMotion]);
  return ref;
}

const MainReplyBubble: React.FC<Omit<MainDetailProps, 'visible'> & { open: boolean; onToggle: () => void; pinnedReply?: WorkbenchMessage | null; reducedMotion: boolean }> = ({ open, onToggle, pinnedReply, reducedMotion, ...props }) => {
  const { t } = useTranslation();
  // A lingering reply keeps its summary even if a later result (a background
  // completion) arrives while it lingers.
  const last = pinnedReply ?? props.exchange.results.at(-1);
  const reply = last?.text?.trim() ?? '';
  const summary = props.running ? t('pet.thinking')
    : props.failure ? `${t('pet.failedPrefix')} ${props.failure.text.trim()}`
        : reply ? collapseMarkdownToPlainText(reply)
          : t('pet.waiting');
  // A status word alone ("thinking", "waiting") sizes the bubble to its
  // content (§4.2); a reply, a failure or the open history take the column.
  const compact = !open && (props.running || (!props.failure && !reply));
  const bubbleRef = useWidthMorph(compact, reducedMotion);
  const detailId = useId();
  const { ref: fadeRef, measure: measureFade, style: fadeStyle } = useScrollEdgeFade(open, HISTORY_FADE);
  // The toggle stays first in the DOM, so focus order is unchanged, but the
  // bubble lays out bottom-up: the toggle keeps its place at the bottom and
  // the history opens above it, so expanding never moves what was clicked.
  return (
    <div ref={bubbleRef} className={clsx('pet-mini-reply-bubble is-main', compact && 'is-compact')} data-pet-hit>
      <button type="button" className="pet-mini-reply-toggle" onClick={onToggle} aria-expanded={open} aria-controls={detailId}>
        <span className="pet-mini-bubble-speaker">{t('pet.mainAgent')}<span>{t(open ? 'pet.collapseHistory' : 'pet.recentTurns')}<ChevronUp size={11} /></span></span>
        <p title={summary}>{props.running && <span className="pet-mini-running-dot" aria-hidden />}<span className="pet-mini-reply-summary">{summary}</span></p>
      </button>
      <div id={detailId} className={clsx('pet-mini-detail', open && 'is-open')}>
        <div className="pet-mini-detail-clip" inert={!open || undefined}>
          {/* Pinned outside the scroll box, so it stays put while the history
              scrolls and is not faded by its edge mask. */}
          {open && <PetOpenButton link={sessionLink(props.binding)} placement="below-end" className="pet-mini-history-open" />}
          <div ref={fadeRef} onScroll={measureFade} className="pet-mini-bubble-history" style={fadeStyle}><MainDetail {...props} visible={open} /></div>
        </div>
      </div>
    </div>
  );
};

const MainDetail: React.FC<MainDetailProps> = (props) => {
  const { t } = useTranslation();
  const innerRef = useRef<HTMLDivElement>(null);
  const lastResultRef = useRef<HTMLDivElement>(null);
  const resultKey = `${props.binding}\u0000${props.exchange.results.at(-1)?.id ?? ''}`;
  const [seenResult, setSeenResult] = useState<{ epoch: PetSurfaceEpoch; key: string } | null>(null);
  // Chat-style: open at the very bottom, where the end of the newest reply,
  // its actions and notices sit right above the toggle row. While open, new
  // content keeps a reader who is at the bottom pinned there, and leaves one
  // who has scrolled up where they are.
  useLayoutEffect(() => {
    if (!props.visible) return undefined;
    const inner = innerRef.current;
    const history = inner?.closest<HTMLElement>('.pet-mini-bubble-history');
    if (!inner || !history) return undefined;
    // The box also shrinks while open (the input below it grows). The scroll
    // event of a pin arrives a frame later, read against a box that may have
    // shrunk since; it is the pin's own, not a reader scrolling up.
    let own = -1;
    const toBottom = () => { history.scrollTop = history.scrollHeight - history.clientHeight; own = history.scrollTop; };
    let pinned = true;
    const onScroll = () => {
      if (history.scrollTop === own) return;
      own = -1;
      pinned = history.scrollHeight - history.clientHeight - history.scrollTop <= HISTORY_STICK_PX;
    };
    toBottom();
    history.addEventListener('scroll', onScroll);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => { if (pinned) toBottom(); });
    observer?.observe(inner);
    observer?.observe(history);
    return () => {
      history.removeEventListener('scroll', onScroll);
      observer?.disconnect();
    };
  }, [props.visible, props.exchange.results.at(-1)?.id]);
  useEffect(() => {
    const node = lastResultRef.current;
    if (!props.visible || !node || typeof IntersectionObserver === 'undefined') return undefined;
    const observer = new IntersectionObserver(([entry]) => {
      if (props.epoch.isCurrent()) setSeenResult(entry.isIntersecting ? { epoch: props.epoch, key: resultKey } : null);
    }, {
      root: node.closest('.pet-mini-bubble-history'),
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [props.visible, resultKey, props.epoch]);
  const resultVisible = props.visible && (typeof IntersectionObserver === 'undefined'
    || (seenResult?.epoch === props.epoch && seenResult.key === resultKey));
  usePetMarkRead(props.epoch, props.binding, props.exchange, props.markRead, props.onRead, resultVisible);
  const lastResult = props.exchange.results.at(-1);
  return (
    <div ref={innerRef} className="pet-mini-detail-inner">
      {props.running && <p className="pet-mini-detail-muted">{t('pet.thinking')}</p>}
      {lastResult && <div ref={lastResultRef} className="pet-mini-detail-result"><MessageContent message={lastResult} /></div>}
      {props.failure && (
        <div className="pet-mini-detail-notice" role="status">
          <p>{props.failure.text}</p>
          {props.failure.metadata?.failure_next_step === 'models' && <Button variant="link" size="sm" className="px-0" onClick={() => void petBridge.open(sessionLink(props.binding))}>{t('pet.fixInAvibe')}</Button>}
        </div>
      )}
      {props.voiceNeedsCloud && <p className="pet-mini-detail-muted">{t('pet.voice.cloudRequired')}</p>}
      {props.queued && <p className="pet-mini-detail-muted">{t('pet.queued')}</p>}
      {/* The pinned Open in Avibe button is where the rest of the tail lives. */}
      {!props.exchange.unreadComplete && <p className="pet-mini-detail-muted">{t('pet.moreInAvibe')}</p>}
      {props.vaultRequestIds.map((id) => <Button key={id} variant="outline" size="sm" className="justify-start gap-2" onClick={() => void petBridge.open(vaultRequestLink(id))}><KeyRound className="size-3.5" />{t('pet.vaultRequest')}</Button>)}
      {props.canChat && !props.uncertain && props.quickReplies && <QuickReplies key={props.quickReplies.message.id} options={props.quickReplies.options} chosen={null} onChoose={(choice) => props.onChoose(props.quickReplies!.message, choice)} />}
      {props.uncertain && <div className="pet-mini-detail-notice" role="status"><p>{t('newSession.sendUncertain')}</p><Button variant="link" size="sm" className="px-0" onClick={props.onInspect}>{t('pet.openInAvibe')}</Button></div>}
    </div>
  );
};

function usePetMarkRead(epoch: PetSurfaceEpoch, binding: string, exchange: ReturnType<typeof latestExchange>, markRead: (sessionId: string, untilMessageId?: string) => Promise<boolean>, onRead: () => void, visible: boolean) {
  const active = usePageActive();
  const last = exchange.results.at(-1);
  const unreadId = last && last.read_at === null ? last.id : null;
  const marker = useRef<string | null>(null);
  useEffect(() => { marker.current = null; }, [epoch]);
  useEffect(() => {
    if (!epoch.isCurrent() || !unreadId || exchange.results.length !== 1 || !exchange.unreadComplete || !active || !visible) return;
    const key = `${binding}\u0000${unreadId}`;
    if (marker.current === key) return;
    marker.current = key;
    void markRead(binding, unreadId).then((applied) => {
      if (!epoch.isCurrent()) return;
      if (applied) onRead(); else marker.current = null;
    }, () => { if (epoch.isCurrent()) marker.current = null; });
  }, [active, binding, exchange.results.length, exchange.unreadComplete, markRead, onRead, unreadId, visible, epoch]);
}
