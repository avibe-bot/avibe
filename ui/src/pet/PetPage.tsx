import { useCallback, useEffect, useMemo, useRef, useState, useLayoutEffect, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ArrowLeftRight, ExternalLink, KeyRound } from 'lucide-react';

import { ApiError, useApi, type SessionActivityState, type WorkbenchMessage } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { Button } from '@/components/ui/button';
import { Markdown } from '@/components/ui/markdown';
import { QuickReplies } from '@/components/workbench/QuickReplies';
import { harnessItemNativeId } from '@/lib/backgroundActivity';
import { usePageActive } from '@/lib/pageActivity';
import { usePendingVaultRequests } from '@/lib/usePendingVaultRequests';
import { ShowPageVoiceDictation } from '@/lib/showPageVoiceDictation';
import { VoiceTranscriptionError } from '@/lib/voiceTranscription';

import { PetAvatar, type PetPose } from './PetAvatar';
import { petBridge, sessionLink, vaultRequestLink } from './petBridge';
import { petPanel, usePetPanel } from './petPanel';
import { petComposer, usePetComposer } from './petComposer';
import { petShell, useOnSummon, usePetBinding, usePetDismissed } from './petShell';
import { derivePetPose, derivePetState, latestExchange, latestFailureNotice, openQuickReplies } from './petState';
import { useConversationAgentWorking } from './useConversationAgentWorking';
import { usePetSession, type PetSurfaceEpoch } from './usePetSession';
import { usePetSetup } from './usePetSetup';
import { useSessionSwitcher } from './useSessionSwitcher';
import './pet-mini.css';

const HOLD_MS = 320;
const DISMISS_EXIT_MS = 500;
const STOP_CONFIRM_MS = 3000;
const STOP_LINGER_MS = 2500;
const STACK_HEIGHT = 304; // 292px scroll box + 6px panel padding on each side.
const EMPTY_ACTIVITIES: SessionActivityState[] = [];
const FADE_PX = 32;
const REFUSED_SEND_STATUSES = new Set([400, 403, 404, 409, 422]);
const sendRefused = (error: unknown) => error instanceof ApiError && REFUSED_SEND_STATUSES.has(error.status);

type CardKind = 'watch' | 'task' | 'agent_run' | 'backend_activity';
type StopPhase = 'confirm' | 'stopping' | 'stopped' | 'failed';
type PetCard = {
  id: string;
  title: string;
  kind: CardKind;
  status: string;
  running: boolean;
  canPause: boolean;
  main?: boolean;
};

const isRunning = (activity: SessionActivityState) => activity.status === 'running' || activity.status === 'processing';
const isPausable = (card: PetCard) => card.kind === 'watch' || card.kind === 'task';

const activityTitle = (activity: SessionActivityState, fallback: string) => (
  (activity.label || activity.description || fallback).trim()
);

const activityStatusKey = (status: string): string => ({
  running: 'pet.activityStatus.running',
  processing: 'pet.activityStatus.running',
  queued: 'pet.activityStatus.queued',
  pending: 'pet.activityStatus.waiting',
  waiting: 'pet.activityStatus.waiting',
  enabled: 'pet.activityStatus.waiting',
  scheduled: 'pet.activityStatus.waiting',
  disabled: 'pet.activityStatus.stopped',
  completed: 'pet.activityStatus.completed',
  succeeded: 'pet.activityStatus.completed',
  failed: 'pet.activityStatus.failed',
  error: 'pet.activityStatus.failed',
}[status] ?? 'pet.activityStatus.unknown');

function activityCard(activity: SessionActivityState, fallback: string, t: (key: string) => string): PetCard {
  const kindKey = `pet.activityKind.${activity.item_kind ?? 'backend_activity'}`;
  return {
    id: activity.id,
    title: activityTitle(activity, fallback),
    kind: activity.item_kind ?? 'backend_activity',
    status: `${t(kindKey)} · ${t(activityStatusKey(activity.status))}`,
    running: isRunning(activity),
    // Durable definitions are pausable while waiting for their next execution.
    // They do not make Vibey busy or receive the actively-running indicator.
    canPause: (activity.item_kind === 'watch' && (activity.status === 'enabled' || isRunning(activity)))
      || (activity.item_kind === 'task' && (activity.status === 'scheduled' || isRunning(activity))),
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
  const rootRef = useRef<HTMLDivElement>(null);
  useHitRegions(rootRef);
  return (
    <div ref={rootRef} className="pet-mini-stage" data-pet-root>
      <div className="pet-mini-setup" data-pet-hit>
        <p>{t('pet.setupPending')}</p>
        <Button size="sm" onClick={() => void petBridge.open('avibe://settings')}>{t('pet.openAvibe')}</Button>
      </div>
      <div className="pet-mini-avatar-wrap" data-pet-hit><PetAvatar pose="idle" /></div>
    </div>
  );
};

const PetSurface: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const rawBinding = usePetBinding();
  const binding = rawBinding ?? null;
  const { capabilities } = useInstanceAuthorization();
  const inbox = useWorkbenchInbox({ feed: false });
  const { expanded, setPanel } = usePetPanel();
  const { draft, uncertain, sending, listenPending, setDraft, setUncertain } = usePetComposer(binding);
  const data = usePetSession(binding, petShell.unbind);
  const epoch = data.epoch;
  const canChat = capabilities.can_chat && data.canChat === true;
  // Interim main-agent rule (spec §10.1): with nothing bound, the mini binds the
  // caller's Vibey conversation, which the server creates on first use. A session
  // it already handed out is never re-bound once it was cleared as invalid, so a
  // bad answer cannot turn into a bind/clear loop.
  const ensured = useRef<{ generation: number; ids: Set<string>; inFlight: boolean }>({ generation: -1, ids: new Set(), inFlight: false });
  useEffect(() => {
    if (rawBinding !== null || !capabilities.can_chat) return;
    const state = ensured.current;
    if (state.generation !== epoch.authorizationGeneration) {
      ensured.current = { generation: epoch.authorizationGeneration, ids: new Set(), inFlight: false };
    }
    if (ensured.current.inFlight) return;
    const current = ensured.current;
    current.inFlight = true;
    void api.ensureDesktopMiniSession().then((session) => {
      if (!epoch.isCurrent() || current.ids.has(session.id)) return;
      current.ids.add(session.id);
      petShell.pick(session.id, epoch.isAuthorizationCurrent);
    }, () => undefined).finally(() => { current.inFlight = false; });
  }, [rawBinding, capabilities.can_chat, api, epoch]);
  const { requests: vaultRequests } = usePendingVaultRequests(binding ?? '');
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
  const activeActivities = data.turn?.background_activities ?? EMPTY_ACTIVITIES;
  const activeSubtasks = activeActivities.filter(isRunning);
  const othersWorking = useConversationAgentWorking(capabilities.can_use_agents === true);
  const busy = data.running || activeSubtasks.length > 0 || othersWorking;
  const [asleep, setAsleep] = useState(false);
  useEffect(() => {
    setAsleep(false);
    if (state !== 'idle' || busy) return;
    const timer = window.setTimeout(() => { if (epoch.isCurrent()) setAsleep(true); }, 5 * 60 * 1000);
    return () => window.clearTimeout(timer);
  }, [state, busy, epoch]);
  const pose: PetPose = derivePetPose(state, { othersWorking: othersWorking || activeSubtasks.length > 0, asleep });
  const [detailOpen, setDetailOpen] = useState(false);
  const [composing, setComposing] = useState(false);
  const [stops, setStops] = useState<Record<string, StopPhase>>({});
  const dismissed = usePetDismissed();
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [optimisticTitle, setOptimisticTitle] = useState('');
  const reducedMotion = useReducedMotion();
  const [leaving, setLeaving] = useState<string[]>([]);
  const [orderedIds, setOrderedIds] = useState<string[]>([]);
  const [retainedCards, setRetainedCards] = useState<Record<string, PetCard>>({});
  const stopTimers = useRef<Record<string, number>>({});
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const pressRef = useRef<{ held: boolean; dragged: boolean; x: number; y: number; timer: number } | null>(null);
  const dismissTimers = useRef<Record<string, number>>({});
  const stopPhases = useRef<Record<string, StopPhase>>({});
  const leavingIds = useRef(new Set<string>());
  const previousBinding = useRef(binding);
  useLayoutEffect(() => {
    setDetailOpen(false);
    setSwitcherOpen(false);
    // Authorization changes remove authorized presentation, not the user's text.
    if (previousBinding.current !== binding) {
      setComposing(false);
      setOptimisticTitle('');
      previousBinding.current = binding;
    }
    setStops({});
    setLeaving([]);
    setOrderedIds([]);
    setRetainedCards({});
    stopPhases.current = {};
    leavingIds.current.clear();
    return () => {
      Object.values(stopTimers.current).forEach(window.clearTimeout);
      Object.values(dismissTimers.current).forEach(window.clearTimeout);
      if (pressRef.current) window.clearTimeout(pressRef.current.timer);
      stopTimers.current = {};
      dismissTimers.current = {};
      pressRef.current = null;
    };
  }, [binding, epoch]);
  useEffect(() => {
    if (!expanded) setDetailOpen(false);
  }, [expanded]);

  const mainCard = useMemo<PetCard | null>(() => {
    const user = exchange.user;
    const latest = exchange.results.at(-1);
    if (!user && !latest && !failure && !optimisticTitle && !(uncertain && draft.trim())) return null;
    const title = optimisticTitle || (uncertain ? draft.trim() : '') || user?.text?.trim() || t('pet.mainAgentTitle');
    const subtitle = data.running
      ? t('pet.thinking')
      : failure
        ? `${t('pet.failedPrefix')} ${failure.text.trim()}`
        : latest?.text?.trim()
        ? `${t('pet.repliedPrefix')} ${latest.text.trim()}`
        : t('pet.waiting');
    return { id: 'main-agent', title, kind: 'backend_activity', status: subtitle, running: data.running, canPause: false, main: true };
  }, [data.running, exchange.results, exchange.user, failure, optimisticTitle, uncertain, draft, t]);
  useEffect(() => {
    if (exchange.user?.text?.trim() === optimisticTitle) setOptimisticTitle('');
  }, [exchange.user, optimisticTitle]);

  const liveCards = useMemo(() => activeActivities.map((activity) => activityCard(activity, t('pet.untitledTask'), (key) => String(t(key as never)))), [activeActivities, t]);
  const activities = useMemo(() => [
    ...liveCards.map((card) => retainedCards[card.id] ?? card),
    ...Object.values(retainedCards).filter((card) => !liveCards.some((live) => live.id === card.id)),
  ], [liveCards, retainedCards]);
  useEffect(() => {
    setOrderedIds((current) => {
      const live = new Set(activities.map((card) => card.id));
      const fresh = activities.filter((card) => !current.includes(card.id)).map((card) => card.id);
      const next = expanded && current.length > 0
        ? [...fresh, ...current.filter((id) => live.has(id))]
        : [...activities].sort((a, b) => Number(b.running) - Number(a.running)).map((card) => card.id);
      return next.length === current.length && next.every((id, i) => id === current[i]) ? current : next;
    });
  }, [activities, expanded]);
  useEffect(() => {
    // A missing initial read or an authorization recheck is not an empty turn.
    if (!data.turn || !binding) return;
    const liveIds = new Set(liveCards.map((card) => card.id));
    const next = dismissed.filter((id) => liveIds.has(id));
    if (next.length !== dismissed.length) petShell.setDismissed(binding, next);
  }, [liveCards, binding, data.turn, dismissed]);

  const sortedActivities = useMemo(() => {
    const byId = new Map(activities.map((card) => [card.id, card]));
    const ordered = orderedIds.map((id) => byId.get(id)).filter((card): card is PetCard => Boolean(card));
    const fresh = activities.filter((card) => !orderedIds.includes(card.id));
    return [...fresh, ...ordered];
  }, [activities, orderedIds]);
  const visibleCards = useMemo(() => [
    ...(mainCard ? [mainCard] : []),
    ...sortedActivities.filter((card) => !dismissed.includes(card.id)),
  ], [dismissed, mainCard, sortedActivities]);

  const closeComposer = useCallback(() => {
    setComposing(false);
    setDraft('');
  }, [setDraft]);
  const openComposer = useCallback((text = '') => {
    if (!epoch.isCurrent()) return;
    setDraft(text);
    setComposing(true);
    window.setTimeout(() => { if (epoch.isCurrent()) inputRef.current?.focus(); }, 0);
  }, [setDraft, epoch]);

  const send = useCallback(async (text: string, metadata?: Record<string, unknown>) => {
    if (!epoch.isCurrent() || !text.trim() || !binding || !canChat || data.session?.id !== binding) return false;
    if (petComposer.isSending() || petComposer.isUncertain(binding)) return false;
    petComposer.setSending(true);
    try {
      const row = await api.sendSessionMessage(binding, { text: text.trim(), ...(metadata ? { metadata } : {}) });
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
  }, [api, binding, canChat, data, setPanel, setUncertain, epoch]);
  const submit = async () => {
    const submitted = draft;
    if (!(await send(submitted)) || !epoch.isCurrent()) return;
    if (petComposer.clearDraftIf(submitted)) setComposing(false);
  };
  const choose = (message: WorkbenchMessage, choice: string) => send(choice, { quick_reply_for: message.id });

  const dismiss = useCallback((id: string) => {
    if (!epoch.isCurrent() || leavingIds.current.has(id) || id === 'main-agent') return;
    leavingIds.current.add(id);
    const card = visibleCards.find((item) => item.id === id);
    if (card) setRetainedCards((current) => ({ ...current, [id]: card }));
    setLeaving((current) => [...current, id]);
    const remove = () => {
      if (!epoch.isCurrent()) return;
      if (binding) {
        petShell.setDismissed(binding, [...new Set([...petShell.dismissed(), id])]);
        setLeaving((current) => current.filter((item) => item !== id));
      }
      leavingIds.current.delete(id);
      setRetainedCards((current) => { const next = { ...current }; delete next[id]; return next; });
    };
    if (reducedMotion) remove();
    else dismissTimers.current[id] = window.setTimeout(remove, DISMISS_EXIT_MS);
  }, [binding, reducedMotion, visibleCards, epoch]);

  const setStop = useCallback((card: PetCard) => {
    if (!epoch.isCurrent() || !card.canPause || card.main) return;
    const updatePhase = (phase?: StopPhase) => {
      if (!epoch.isCurrent()) return;
      if (phase) stopPhases.current[card.id] = phase;
      else delete stopPhases.current[card.id];
      setStops({ ...stopPhases.current });
    };
    const phase = stopPhases.current[card.id];
    if (!phase || phase === 'failed') {
      updatePhase('confirm');
      window.clearTimeout(stopTimers.current[card.id]);
      stopTimers.current[card.id] = window.setTimeout(() => updatePhase(), STOP_CONFIRM_MS);
      return;
    }
    if (phase !== 'confirm') return;
    window.clearTimeout(stopTimers.current[card.id]);
    updatePhase('stopping');
    setRetainedCards((current) => ({ ...current, [card.id]: card }));
    const update = card.kind === 'watch' ? api.setHarnessWatchEnabled(harnessItemNativeId(card), false) : api.setHarnessTaskEnabled(harnessItemNativeId(card), false);
    void update.then((result) => {
      if (!epoch.isCurrent()) return;
      if (!result.ok) throw new Error('stop failed');
      updatePhase('stopped');
      stopTimers.current[card.id] = window.setTimeout(() => dismiss(card.id), STOP_LINGER_MS);
    }).catch(() => {
      if (!epoch.isCurrent()) return;
      updatePhase('failed');
      setRetainedCards((current) => { const next = { ...current }; delete next[card.id]; return next; });
    });
  }, [api, dismiss, epoch]);

  const voice = usePetVoice({
    enabled: canChat && Boolean(binding) && !uncertain,
    epoch,
    draft,
    onOpenComposer: openComposer,
  });

  const clearVoiceError = voice.clearError;
  useEffect(() => { if (!composing) clearVoiceError(); }, [composing, clearVoiceError]);

  const hotkeyHeld = useRef(false);
  const onSummon = useCallback(() => {
    const intent = petShell.takeSummon();
    if (!intent) return;
    if (intent === 'release') {
      hotkeyHeld.current = false;
      voice.stop(true);
      if (!voice.listening && !composing) {
        void setPanel(true);
        petComposer.requestListen();
      }
      return;
    }
    void setPanel(true);
    setSwitcherOpen(false);
    if (intent === 'listen') {
      hotkeyHeld.current = true;
      petComposer.requestListen();
    }
  }, [setPanel, voice.stop, voice.listening, composing]);
  useOnSummon(onSummon);
  useEffect(() => {
    if (!listenPending || !binding || !canChat || !expanded) return;
    petComposer.takeListen();
    if (uncertain) { setDetailOpen(true); return; }
    if (hotkeyHeld.current && voice.available) voice.start();
    else openComposer(draft);
  }, [listenPending, binding, canChat, expanded, voice.available, voice.start, openComposer, draft, uncertain]);
  useEffect(() => {
    const onBlur = () => { if (!voice.listening && !draft.trim()) void setPanel(false); };
    window.addEventListener('blur', onBlur);
    return () => window.removeEventListener('blur', onBlur);
  }, [voice.listening, draft, setPanel]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (voice.listening) voice.cancel();
      else if (composing) closeComposer();
      else if (switcherOpen) setSwitcherOpen(false);
      else void setPanel(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [closeComposer, composing, setPanel, switcherOpen, voice]);

  useEffect(() => {
    const node = inputRef.current;
    if (!node) return;
    node.style.height = 'auto';
    node.style.height = `${Math.min(node.scrollHeight, 96)}px`;
  }, [draft, composing]);

  const rootRef = useRef<HTMLDivElement>(null);
  useHitRegions(rootRef, epoch);
  const collapsedRef = useRef<HTMLDivElement>(null);
  const [collapsedHeight, setCollapsedHeight] = useState(0);
  useLayoutEffect(() => {
    const node = collapsedRef.current;
    if (!node || expanded) return;
    const measure = () => setCollapsedHeight(node.getBoundingClientRect().height);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node);
    return () => observer?.disconnect();
  }, [expanded, visibleCards.length]);

  const avatar = (
    <div className={clsx('pet-mini-avatar-wrap', voice.listening && 'is-listening')}>
      {voice.listening && <span className="pet-mini-listen-ring" aria-hidden />}
      <button
        type="button"
        className="pet-mini-avatar-button"
        aria-label={t('pet.toggle')}
        aria-expanded={expanded}
        data-pet-hit
        onClick={(event) => { if (event.detail === 0) void setPanel(!petPanel.want()); }}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          const target = event.currentTarget;
          const pointerId = event.pointerId;
          pressRef.current = { held: false, dragged: false, x: event.clientX, y: event.clientY, timer: window.setTimeout(() => {
            if (!epoch.isCurrent()) return;
            const press = pressRef.current;
            if (!press) return;
            press.held = true;
            target.setPointerCapture?.(pointerId);
            if (!canChat || !binding) { void setPanel(true); return; }
            if (uncertain) { void setPanel(true); setDetailOpen(true); return; }
            if (voice.available) voice.start();
            else openComposer(draft);
          }, HOLD_MS) };
        }}
        onPointerMove={(event) => {
          const press = pressRef.current;
          if (!press || press.held || press.dragged || event.buttons !== 1) return;
          if (Math.hypot(event.clientX - press.x, event.clientY - press.y) < 4) return;
          press.dragged = true;
          window.clearTimeout(press.timer);
          void petBridge.startDragging();
        }}
        onPointerCancel={() => {
          const press = pressRef.current;
          pressRef.current = null;
          if (press) window.clearTimeout(press.timer);
          if (press?.held) voice.stop(true);
        }}
        onPointerUp={() => {
          const press = pressRef.current;
          pressRef.current = null;
          if (!press) return;
          window.clearTimeout(press.timer);
          if (press.held) voice.stop(true);
          else if (!press.dragged) void setPanel(!petPanel.want());
        }}
        onPointerLeave={() => {
          const press = pressRef.current;
          if (press && !press.held) {
            window.clearTimeout(press.timer);
            pressRef.current = null;
          }
        }}
        onContextMenu={(event) => event.preventDefault()}
      >
        <PetAvatar pose={voice.listening ? 'listening' : pose} badge={state === 'ready' ? unreadCount : 0} activities={activeSubtasks.length} />
      </button>
      <MiniDock
        enabled={canChat && Boolean(binding)}
        listening={voice.listening}
        typing={composing}
        // Availability gates starting a capture; an active one always keeps its stop control.
        showMic={voice.available || voice.listening}
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
    <div ref={rootRef} className="pet-mini-stage" data-pet-root data-busy={busy} data-state={state}>
      <div className={clsx('pet-mini-column', voice.listening && 'is-voice-active')}>
        <div className={clsx('pet-mini-cards', voice.listening && 'is-hidden')} inert={voice.listening || undefined} aria-label={expanded ? t('pet.panel') : undefined} style={{ height: expanded ? STACK_HEIGHT : collapsedHeight }}>
          <div ref={collapsedRef} key={expanded ? 'expanded' : 'collapsed'} className="pet-mini-swap is-switching" onAnimationEnd={(event) => { if (event.target === event.currentTarget) event.currentTarget.classList.remove('is-switching'); }}>
          {expanded && (!binding || switcherOpen || visibleCards.length === 0) ? (
            <SessionPicker canStartVibey={capabilities.can_chat} onPick={(sessionId) => {
              if (!epoch.isCurrent()) return;
              petShell.pick(sessionId, epoch.isAuthorizationCurrent);
              setSwitcherOpen(false);
            }} />
          ) : visibleCards.length > 0 && (
            expanded ? (
              <ExpandedCards
                cards={visibleCards}
                leaving={leaving}
                detailOpen={detailOpen}
                onMain={() => setDetailOpen((open) => !open)}
                onSwitch={mainCard ? undefined : () => setSwitcherOpen(true)}
                onDismiss={dismiss}
                stops={stops}
                onStop={setStop}
                t={(key, options) => String(t(key as never, options as never))}
                detail={binding ? <MainDetail epoch={epoch} voiceNeedsCloud={voice.cloudRequired} canChat={canChat} visible={detailOpen && !voice.listening} onSwitch={() => setSwitcherOpen(true)} binding={binding} exchange={exchange} failure={data.running ? null : failure} running={data.running} queued={(data.turn?.pending_input_count ?? 0) > 0} markRead={inbox.markRead} onRead={data.refreshTail} vaultRequestIds={vaultRequests.map((request) => request.id)} quickReplies={quickReplies} onChoose={choose} uncertain={uncertain} onInspect={() => { void petBridge.open(sessionLink(binding)).then(() => { if (epoch.isCurrent()) setUncertain(binding, false); }).catch(() => undefined); }} /> : null}
              />
            ) : (
              <CollapsedCards cards={visibleCards} onOpen={() => void setPanel(true)} t={(key) => String(t(key as never))} />
            )
          )}
          </div>
        </div>
        <SpeechSlot open={voice.listening || composing}>
          {voice.listening ? <Marquee text={voice.preview} listeningLabel={t('pet.voice.listening')} /> : canChat && !uncertain ? (
          <>
          {voice.error && <p className="pet-mini-input-note" role="status">{t(`pet.voice.${voice.error}`)}</p>}
          <form className="pet-mini-input" data-pet-hit onSubmit={(event) => { event.preventDefault(); void submit(); }}>
            <textarea
              ref={inputRef}
              rows={1}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void submit();
                }
              }}
              placeholder={t('pet.inputPlaceholder')}
              aria-label={t('pet.inputPlaceholder')}
            />
            {voice.available && <button type="button" className="pet-mini-input-button" onClick={() => voice.start()} aria-label={t('pet.voiceContinue')}><MicIcon /></button>}
            <button type="submit" className="pet-mini-send" disabled={!draft.trim() || sending || uncertain} aria-label={t('pet.send')}><SendIcon /></button>
          </form>
          </>
          ) : null}
        </SpeechSlot>
      </div>
      {avatar}
    </div>
  );
};

const SwitchSessionButton: React.FC<{ onClick: () => void; label: string }> = ({ onClick, label }) => (
  <button type="button" className="pet-mini-chip" data-pet-hit aria-label={label} onClick={onClick}><ArrowLeftRight aria-hidden />{label}</button>
);

const SessionPicker: React.FC<{ onPick: (sessionId: string) => void; canStartVibey: boolean }> = ({ onPick, canStartVibey }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { sessions, loading } = useSessionSwitcher(true);
  const [vibeyError, setVibeyError] = useState<string | null>(null);
  const [vibeyPending, setVibeyPending] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const pickVibey = () => {
    if (vibeyPending) return;
    setVibeyPending(true);
    setVibeyError(null);
    api.ensureDesktopMiniSession().then((session) => {
      if (mounted.current) onPick(session.id);
    }, (error: unknown) => {
      if (mounted.current) setVibeyError(error instanceof ApiError && error.code === 'no_chat_project' ? t('pet.noChatProject') : t('pet.vibeyUnavailable'));
    }).finally(() => { if (mounted.current) setVibeyPending(false); });
  };
  return (
    <div className="pet-mini-panel" aria-label={t('pet.chooseSession')}>
      <div className="pet-mini-scroll">
        <div className="pet-mini-session-picker" data-pet-hit>
          {canStartVibey && <button type="button" onClick={pickVibey} disabled={vibeyPending} className="pet-mini-session-option is-vibey">{t('pet.talkToVibey')}</button>}
          {vibeyError && <p className="pet-mini-detail-muted">{vibeyError}</p>}
          {sessions.length === 0 && <p className="pet-mini-detail-muted">{loading ? t('common.loading') : t('pet.noSessions')}</p>}
          {sessions.map((session) => (
            <button key={session.id} type="button" onClick={() => onPick(session.id)} className="pet-mini-session-option">
              {session.title || t('pet.untitled')}
            </button>
          ))}
        </div>
      </div>
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
        return right > left && bottom > top ? [{ x: left, y: top, width: right - left, height: bottom - top }] : [];
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
    root.addEventListener('scroll', schedule, true);
    root.addEventListener('transitionrun', motion);
    root.addEventListener('pointerover', motion);
    root.addEventListener('pointerout', motion);
    observe();
    return () => {
      active = false;
      cancelAnimationFrame(frame); resize?.disconnect(); mutations.disconnect();
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

function usePetVoice({ enabled, epoch, draft, onOpenComposer }: { enabled: boolean; epoch: PetSurfaceEpoch; draft: string; onOpenComposer: (text?: string) => void }) {
  const [asr, setAsr] = useState<{ epoch: PetSurfaceEpoch; enabled: boolean; available: boolean; checked: boolean; maxFileBytes?: number }>({ epoch, enabled, available: false, checked: false });
  const [preview, setPreview] = useState('');
  const [listening, setListening] = useState(false);
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
    capture.ending = true;
    if (commit) capture.dictation.finish();
    else {
      capture.cancelled = true;
      captureRef.current = null;
      capture.dictation.abort();
      setListening(false);
      setPreview('');
    }
  }, []);
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
    setError(null);
    const settle = () => {
      if (captureRef.current !== capture || !epoch.isCurrent()) return false;
      captureRef.current = null;
      setListening(false);
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
  }, [asrForEpoch.maxFileBytes, available, epoch, draft, onOpenComposer]);
  const cancel = useCallback(() => stop(false), [stop]);
  const clearError = useCallback(() => setError(null), []);
  useEffect(() => cancel, [epoch, enabled, cancel]);
  useEffect(() => () => setError(null), [epoch, enabled]);
  return { available, cloudRequired: asrForEpoch.checked && !asrForEpoch.available, listening, preview, error, clearError, start, stop, cancel };
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

const MiniDock: React.FC<{ enabled: boolean; listening: boolean; typing: boolean; showMic: boolean; onMic: () => void; onKeyboard: () => void; t: (key: string) => string }> = ({ enabled, listening, typing, showMic, onMic, onKeyboard, t }) => (
  <div className={clsx('pet-mini-dock', (listening || typing) && 'is-active')} data-pet-hit>
    {showMic && <button type="button" className={clsx(listening && 'is-listening')} onClick={onMic} aria-pressed={listening} aria-label={listening ? t('pet.voice.stop') : t('pet.voice.open')}>{listening ? <StopIcon /> : <MicIcon />}</button>}
    {showMic && <span className="pet-mini-dock-divider" aria-hidden />}
    <button type="button" className={clsx(typing && 'is-typing')} disabled={!enabled} onClick={onKeyboard} aria-pressed={typing} aria-label={typing ? t('pet.keyboard.close') : t('pet.keyboard.open')}><KeyboardIcon /></button>
  </div>
);

const CollapsedCards: React.FC<{ cards: PetCard[]; onOpen: () => void; t: (key: string) => string }> = ({ cards, onOpen, t }) => {
  const [front, ...layers] = cards;
  if (!front) return null;
  return (
    <button type="button" className="pet-mini-collapsed" style={{ paddingBottom: layers.length * 9 }} onClick={onOpen} aria-label={t('pet.stack.expand')}>
      {layers.map((card, index) => {
        const inset = 18 + 6 + index * 3;
        return <span key={card.id} data-pet-hit className="pet-mini-layer" style={{ top: (index + 1) * 9, bottom: (layers.length - index - 1) * 9, left: inset, right: inset, zIndex: layers.length - index }} aria-hidden />;
      })}
      <div className="pet-mini-front" style={{ zIndex: layers.length + 1 }}><CardBody card={front} expanded={false} t={t} /></div>
    </button>
  );
};

const ExpandedCards: React.FC<{ cards: PetCard[]; leaving: string[]; detailOpen: boolean; onMain: () => void; onSwitch?: () => void; onDismiss: (id: string) => void; stops: Record<string, StopPhase>; onStop: (card: PetCard) => void; t: (key: string, options?: Record<string, unknown>) => string; detail: ReactNode }> = ({ cards, leaving, detailOpen, onMain, onSwitch, onDismiss, stops, onStop, t, detail }) => {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ top: 0, bottom: 0 });
  const measure = useCallback(() => {
    const node = scrollRef.current;
    if (!node) return;
    setEdges({ top: Math.min(FADE_PX, Math.max(0, node.scrollTop)), bottom: Math.min(FADE_PX, Math.max(0, node.scrollHeight - node.clientHeight - node.scrollTop)) });
  }, []);
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return undefined;
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    const list = node.querySelector('.pet-mini-list');
    if (list) observer.observe(list);
    return () => observer.disconnect();
  }, [measure, cards.length, detailOpen]);
  const mask = `linear-gradient(to bottom, transparent 0, #000 ${edges.top}px, #000 calc(100% - ${edges.bottom}px), transparent 100%)`;
  return (
    <div className="pet-mini-panel">
      <div ref={scrollRef} onScroll={measure} className="pet-mini-scroll" style={{ maskImage: mask, WebkitMaskImage: mask }}>
        {onSwitch && <div className="pet-mini-action-row"><SwitchSessionButton onClick={onSwitch} label={t('pet.switchSession')} /></div>}
        <ul className="pet-mini-list">
          {cards.map((card) => {
            const phase = stops[card.id];
            return (
              <li key={card.id} className={clsx('pet-mini-row', leaving.includes(card.id) && 'is-leaving')}>
                <div className="pet-mini-row-inner"><div className={clsx('pet-mini-row-body', card.main && 'is-main')}>
                  <div className={clsx('pet-mini-card-wrap', card.main && 'is-main')} data-pet-hit>
                    <CardBody card={card} expanded t={t} phase={phase} onDismiss={() => onDismiss(card.id)} onStop={() => onStop(card)} onClick={card.main ? onMain : undefined} />
                    {card.main && <div className={clsx('pet-mini-detail', detailOpen && 'is-open')}><div className="pet-mini-detail-clip" inert={!detailOpen || undefined}>{detail}</div></div>}
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

const CardBody: React.FC<{ card: PetCard; expanded: boolean; t: (key: string, options?: Record<string, unknown>) => string; phase?: StopPhase; onDismiss?: () => void; onStop?: () => void; onClick?: () => void }> = ({ card, expanded, t, phase, onDismiss, onStop, onClick }) => {
  const running = card.running && phase !== 'stopped' && phase !== 'stopping';
  const subtitle = phase === 'stopping' ? t(isPausable(card) ? 'pet.stop.pausing' : 'pet.stop.stopping') : phase === 'stopped' ? t(isPausable(card) ? 'pet.stop.paused' : 'pet.stop.stopped') : phase === 'failed' ? t('pet.stop.failed') : card.status;
  const stopText = isPausable(card) ? t('pet.stop.confirmPause') : t('pet.stop.confirmStop');
  return (
    <article className={clsx('pet-mini-card', card.main && 'is-main')} title={card.title} data-pet-hit onClick={onClick} role={onClick ? 'button' : undefined} tabIndex={onClick ? 0 : undefined} onKeyDown={onClick ? (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onClick(); } } : undefined}>
      {expanded && !card.main && <button type="button" className="pet-mini-dismiss" data-pet-hit onClick={(event) => { event.stopPropagation(); onDismiss?.(); }} aria-label={t('pet.dismiss')}><svg viewBox="0 0 16 16" aria-hidden><path d="m4 4 8 8M12 4 4 12" /></svg></button>}
      <div className="pet-mini-card-content">
        {card.main && <div className="pet-mini-eyebrow">{t('pet.mainAgent')}</div>}
        <div className="pet-mini-title">{card.title}</div>
        <div className={clsx('pet-mini-subtitle', phase === 'stopped' && 'is-stopped')} title={subtitle}>
          {running && <span className="pet-mini-running-dot" aria-hidden />}
          {phase === 'stopped' && <span className="pet-mini-stopped-dot" aria-hidden />}
          <span>{subtitle}</span>
        </div>
      </div>
      {expanded && card.canPause && !card.main && phase !== 'stopped' && (
        <button type="button" className={clsx('pet-mini-stop', phase === 'confirm' && 'is-confirm')} onClick={(event) => { event.stopPropagation(); onStop?.(); }} disabled={phase === 'stopping'} aria-label={stopText}>
          {phase === 'confirm' ? stopText : phase === 'stopping' ? <span className="pet-mini-spinner" aria-hidden /> : <StopIcon />}
        </button>
      )}
    </article>
  );
};

const MainDetail: React.FC<{ epoch: PetSurfaceEpoch; voiceNeedsCloud: boolean; canChat: boolean; visible: boolean; onSwitch: () => void; binding: string; exchange: ReturnType<typeof latestExchange>; failure: WorkbenchMessage | null; running: boolean; queued: boolean; markRead: (sessionId: string, untilMessageId?: string) => Promise<boolean>; onRead: () => void; vaultRequestIds: string[]; quickReplies: ReturnType<typeof openQuickReplies>; onChoose: (message: WorkbenchMessage, choice: string) => Promise<boolean>; uncertain: boolean; onInspect: () => void }> = (props) => {
  const { t } = useTranslation();
  const lastResultRef = useRef<HTMLDivElement>(null);
  const resultKey = `${props.binding}\u0000${props.exchange.results.at(-1)?.id ?? ''}`;
  const [seenResult, setSeenResult] = useState<{ epoch: PetSurfaceEpoch; key: string } | null>(null);
  useEffect(() => {
    const node = lastResultRef.current;
    if (!props.visible || !node) return;
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(([entry]) => {
      if (props.epoch.isCurrent()) setSeenResult(entry.isIntersecting ? { epoch: props.epoch, key: resultKey } : null);
    }, {
      root: node.closest('.pet-mini-scroll'),
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [props.visible, resultKey, props.epoch]);
  usePetMarkRead(props.epoch, props.binding, props.exchange, props.markRead, props.onRead, props.visible && (typeof IntersectionObserver === 'undefined' || (seenResult?.epoch === props.epoch && seenResult.key === resultKey)));
  return (
    <div className="pet-mini-detail-inner">
      <div className="pet-mini-action-row"><SwitchSessionButton onClick={props.onSwitch} label={t('pet.switchSession')} /></div>
      {props.exchange.user && <div className="pet-mini-detail-user"><Markdown content={props.exchange.user.text} softBreaks /></div>}
      {props.exchange.results.map((message, index) => <div ref={index === props.exchange.results.length - 1 ? lastResultRef : undefined} key={message.id} className="pet-mini-detail-result"><Markdown content={message.text} softBreaks /></div>)}
      {props.failure && (
        <div className="pet-mini-detail-notice" role="status">
          <p>{props.failure.text}</p>
          {props.failure.metadata?.failure_next_step === 'models' && <Button variant="link" size="sm" className="px-0" onClick={() => void petBridge.open(sessionLink(props.binding))}>{t('pet.fixInAvibe')}</Button>}
        </div>
      )}
      {props.running && <p className="pet-mini-detail-muted">{t('pet.thinking')}</p>}
      {props.voiceNeedsCloud && <p className="pet-mini-detail-muted">{t('pet.voice.cloudRequired')}</p>}
      {props.queued && <p className="pet-mini-detail-muted">{t('pet.queued')}</p>}
      {!props.exchange.unreadComplete && <Button variant="link" size="sm" className="px-0" onClick={() => void petBridge.open(sessionLink(props.binding))}>{t('pet.moreInAvibe')}</Button>}
      {props.vaultRequestIds.map((id) => <Button key={id} variant="outline" size="sm" className="justify-start gap-2" onClick={() => void petBridge.open(vaultRequestLink(id))}><KeyRound className="size-3.5" />{t('pet.vaultRequest')}</Button>)}
      {props.canChat && !props.uncertain && props.quickReplies && <QuickReplies key={props.quickReplies.message.id} options={props.quickReplies.options} chosen={null} onChoose={(choice) => props.onChoose(props.quickReplies!.message, choice)} />}
      {props.uncertain && <div className="pet-mini-detail-notice" role="status"><p>{t('newSession.sendUncertain')}</p><Button variant="link" size="sm" className="px-0" onClick={props.onInspect}>{t('pet.openInAvibe')}</Button></div>}
      <Button variant="link" size="sm" className="px-0" onClick={() => void petBridge.open(sessionLink(props.binding))}><ExternalLink className="mr-1 size-3.5" />{t('pet.openInAvibe')}</Button>
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
    if (!epoch.isCurrent() || !unreadId || !exchange.unreadComplete || !active || !visible) return;
    const key = `${binding}\u0000${unreadId}`;
    if (marker.current === key) return;
    marker.current = key;
    void markRead(binding, unreadId).then((applied) => {
      if (!epoch.isCurrent()) return;
      if (applied) onRead(); else marker.current = null;
    }, () => { if (epoch.isCurrent()) marker.current = null; });
  }, [active, binding, exchange.unreadComplete, markRead, onRead, unreadId, visible, epoch]);
}
