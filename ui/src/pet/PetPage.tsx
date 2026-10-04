import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Activity, ArrowUp, ExternalLink, KeyRound, Shuffle } from 'lucide-react';

import { useApi, type SessionActivityState, type WorkbenchMessage } from '@/context/ApiContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { Button } from '@/components/ui/button';
import { Markdown } from '@/components/ui/markdown';
import { QuickReplies } from '@/components/workbench/QuickReplies';
import { StatusPill } from '@/components/visual';
import {
  activityKindI18nKey,
  resolveActivityLabel,
  sortBackgroundActivities,
} from '@/lib/backgroundActivity';
import { usePendingVaultRequests } from '@/lib/usePendingVaultRequests';
import { useLatestRef } from '@/lib/useLatestRef';

import { PetAvatar, type PetPose } from './PetAvatar';
import {
  SETTINGS_LINK,
  petBridge,
  sessionLink,
  vaultRequestLink,
  type PetIntent,
  type PetLayout,
} from './petBridge';
import { petShell, useOnSummon, usePetBinding } from './petShell';
import { derivePetState, latestExchange, openQuickReplies } from './petState';
import { usePetSession } from './usePetSession';
import { usePetSetup } from './usePetSetup';
import { useSessionSwitcher } from './useSessionSwitcher';

// An idle pet falls asleep after this long without a state change.
const SLEEP_AFTER_MS = 5 * 60 * 1000;
// A pointer that moves this far after pressing the pet drags the window.
const DRAG_THRESHOLD_PX = 4;

/**
 * `/pet`: the desktop pet window's page. Outside the Workbench shell and the
 * setup redirect; see docs/plans/2026-10-01-desktop-pet.md.
 */
export const PetPage: React.FC = () => {
  useLayoutEffect(() => {
    document.documentElement.classList.add('pet-window');
    return () => document.documentElement.classList.remove('pet-window');
  }, []);

  // Listen to the shell from the first frame, before setup is known.
  usePetBinding();
  const { setup, recheck } = usePetSetup();
  if (setup === 'checking') return null;
  if (setup === 'pending') return <PetSetupPending recheck={recheck} />;
  return <PetSurface />;
};

const PetSetupPending: React.FC<{ recheck: () => void }> = ({ recheck }) => {
  const { t } = useTranslation();
  // A summon re-reads setup but stays waiting, so the pet surface acts on it
  // once setup turns out to be complete.
  useOnSummon(recheck);
  return (
    <div className="flex h-full flex-col items-end justify-end gap-2 p-2">
      <div className="max-w-[220px] rounded-xl border border-border bg-card p-3 text-[12px] text-foreground shadow-lg">
        <p className="mb-2">{t('pet.setupPending')}</p>
        <Button size="sm" onClick={() => void petBridge.open(SETTINGS_LINK)}>
          {t('pet.openAvibe')}
        </Button>
      </div>
      <PetAvatar pose="idle" />
    </div>
  );
};

const PetSurface: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const inbox = useWorkbenchInbox({ feed: false });

  const binding = usePetBinding() ?? null;
  const bindingRef = useLatestRef(binding);
  const [expanded, setExpanded] = useState(false);
  const [layout, setLayout] = useState<PetLayout>({ panel_side: 'left', panel_edge: 'bottom' });
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Compare-and-clear, in this page and in the shell.
  const unbind = useCallback((sessionId: string) => {
    if (bindingRef.current === sessionId) petShell.setBinding(null);
    void petBridge.unbind(sessionId).catch(() => undefined);
  }, [bindingRef]);

  const data = usePetSession(binding, unbind);
  const { requests: vaultRequests } = usePendingVaultRequests(binding ?? '');
  const unreadCount = binding ? inbox.unreadBySession[binding] ?? 0 : 0;

  const state = derivePetState({
    session: data.session,
    messages: data.messages,
    turn: data.running ? { foreground: 'running', in_flight: true } : data.turn,
    pendingVaultRequests: vaultRequests.length,
    unreadCount,
  });

  const setPanel = useCallback(async (next: boolean) => {
    setExpanded(next);
    try {
      setLayout(await petBridge.setExpanded(next));
    } catch {
      /* the shell keeps its current frame */
    }
  }, []);

  const summon = useCallback((intent: PetIntent, bound: string | null) => {
    void setPanel(true);
    if (!bound) {
      setSwitcherOpen(true);
      return;
    }
    // Voice ships in a later PR; until then a `listen` summon focuses the text
    // input, and a `show` summon only shows the panel.
    if (intent === 'listen') window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [setPanel]);

  // Act on a waiting summon, including one that arrived before this surface
  // mounted (while setup was pending or the page was loading).
  const onSummon = useCallback(() => {
    const intent = petShell.takeSummon();
    if (intent) summon(intent, bindingRef.current);
  }, [summon, bindingRef]);
  useOnSummon(onSummon);

  // Esc collapses.
  useEffect(() => {
    if (!expanded) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') void setPanel(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [expanded, setPanel]);

  // Blur collapses, unless there is unsent text.
  useEffect(() => {
    if (!expanded) return undefined;
    const onBlur = () => {
      if (!draft.trim()) void setPanel(false);
    };
    window.addEventListener('blur', onBlur);
    return () => window.removeEventListener('blur', onBlur);
  }, [expanded, draft, setPanel]);

  // Idle → asleep after a while without a state change.
  const [asleep, setAsleep] = useState(false);
  useEffect(() => {
    setAsleep(false);
    if (state !== 'idle') return undefined;
    const timer = window.setTimeout(() => setAsleep(true), SLEEP_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [state, binding]);
  const pose: PetPose = state === 'idle' && asleep ? 'sleeping' : state;

  const exchange = useMemo(() => latestExchange(data.messages, data.hasOlder), [data.messages, data.hasOlder]);
  const quickReplies = openQuickReplies(data.messages);

  // Mark read only what the panel rendered, and only when every unread result
  // is in the loaded tail; otherwise reading is left to the Workbench.
  const lastRenderedUnread = exchange.results.length > 0 && exchange.results[exchange.results.length - 1].read_at === null
    ? exchange.results[exchange.results.length - 1].id
    : null;
  useEffect(() => {
    if (!expanded || !binding || !lastRenderedUnread || !exchange.unreadComplete) return;
    if (document.visibilityState !== 'visible') return;
    void inbox.markRead(binding, lastRenderedUnread).catch(() => undefined);
  }, [expanded, binding, lastRenderedUnread, exchange.unreadComplete, inbox]);

  const send = useCallback(async (text: string, metadata?: Record<string, unknown>) => {
    if (!binding || !text.trim()) return false;
    setSending(true);
    try {
      const row = await api.sendSessionMessage(binding, { text, ...(metadata ? { metadata } : {}) });
      data.noteSent(row && typeof row === 'object' && 'id' in row ? row : null);
      return true;
    } catch {
      return false;
    } finally {
      setSending(false);
    }
  }, [api, binding, data]);

  const submit = async () => {
    const text = draft.trim();
    if (!text) return;
    if (await send(text)) setDraft('');
  };

  // QuickReplies locks the group locally; the Runtime's message.updated for the
  // answered row then clears Needs input in every window.
  const choose = (message: WorkbenchMessage, choice: string) => send(choice, { quick_reply_for: message.id });

  const pressRef = useRef<{ x: number; y: number; dragged: boolean } | null>(null);
  const avatar = (
    <button
      type="button"
      className="shrink-0 cursor-pointer rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring"
      aria-label={t('pet.toggle')}
      aria-expanded={expanded}
      onPointerDown={(event) => {
        pressRef.current = { x: event.clientX, y: event.clientY, dragged: false };
      }}
      onPointerMove={(event) => {
        const press = pressRef.current;
        if (!press || press.dragged || event.buttons !== 1) return;
        if (Math.hypot(event.clientX - press.x, event.clientY - press.y) < DRAG_THRESHOLD_PX) return;
        press.dragged = true;
        petBridge.startDragging();
      }}
      onClick={() => {
        const dragged = pressRef.current?.dragged;
        pressRef.current = null;
        if (dragged) return;
        if (!binding && !expanded) setSwitcherOpen(true);
        void setPanel(!expanded);
      }}
    >
      <PetAvatar pose={pose} badge={state === 'ready' ? unreadCount : 0} />
    </button>
  );

  const panel = expanded ? (
    <PetPanel
      binding={binding}
      title={data.session?.title ?? null}
      switcherOpen={switcherOpen || !binding}
      onToggleSwitcher={() => setSwitcherOpen((open) => !open)}
      onPick={(sessionId) => {
        setSwitcherOpen(false);
        petShell.setBinding(sessionId);
        void petBridge.bind(sessionId).catch(() => undefined);
      }}
      exchange={exchange}
      running={data.running}
      queued={(data.turn?.pending_input_count ?? 0) > 0}
      activities={data.turn?.background_activities ?? []}
      vaultRequestIds={vaultRequests.map((request) => request.id)}
      quickReplies={quickReplies}
      onChoose={choose}
      draft={draft}
      onDraft={setDraft}
      onSubmit={() => void submit()}
      sending={sending}
      inputRef={inputRef}
    />
  ) : null;

  return (
    <div
      className={clsx(
        'flex h-full w-full gap-2 p-2',
        layout.panel_side === 'left' ? 'flex-row' : 'flex-row-reverse',
        layout.panel_edge === 'bottom' ? 'items-end' : 'items-start',
      )}
      data-state={state}
    >
      {panel}
      {avatar}
    </div>
  );
};

type PanelProps = {
  binding: string | null;
  title: string | null;
  switcherOpen: boolean;
  onToggleSwitcher: () => void;
  onPick: (sessionId: string) => void;
  exchange: ReturnType<typeof latestExchange>;
  running: boolean;
  queued: boolean;
  activities: SessionActivityState[];
  vaultRequestIds: string[];
  quickReplies: ReturnType<typeof openQuickReplies>;
  onChoose: (message: WorkbenchMessage, choice: string) => Promise<boolean>;
  draft: string;
  onDraft: (value: string) => void;
  onSubmit: () => void;
  sending: boolean;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
};

const PetPanel: React.FC<PanelProps> = ({ inputRef, ...props }) => {
  const { t } = useTranslation();
  const { binding } = props;
  return (
    <section
      className="flex max-h-full w-[320px] min-w-0 flex-col overflow-hidden rounded-2xl border border-border bg-card text-foreground shadow-xl"
      aria-label={t('pet.panel')}
    >
      <header className="flex items-center gap-1.5 border-b border-border px-3 py-2">
        <span className="min-w-0 flex-1 truncate text-[12px] font-medium">
          {binding ? (props.title || t('pet.untitled')) : t('pet.chooseSession')}
        </span>
        {binding && (
          <Button variant="ghost" size="icon" className="size-7" aria-label={t('pet.switchSession')} onClick={props.onToggleSwitcher}>
            <Shuffle className="size-3.5" />
          </Button>
        )}
        {binding && (
          <Button variant="ghost" size="icon" className="size-7" aria-label={t('pet.openInAvibe')} onClick={() => void petBridge.open(sessionLink(binding))}>
            <ExternalLink className="size-3.5" />
          </Button>
        )}
      </header>
      {props.switcherOpen ? (
        <SessionSwitcher current={binding} onPick={props.onPick} />
      ) : binding ? (
        <>
          <ExchangeView exchange={props.exchange} running={props.running} binding={binding} />
          <ActivityLine activities={props.activities} />
          <NeedsInput
            vaultRequestIds={props.vaultRequestIds}
            quickReplies={props.quickReplies}
            onChoose={props.onChoose}
          />
          <form
            className="flex items-end gap-1.5 border-t border-border p-2"
            onSubmit={(event) => {
              event.preventDefault();
              props.onSubmit();
            }}
          >
            <textarea
              ref={inputRef}
              rows={1}
              value={props.draft}
              onChange={(event) => props.onDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  props.onSubmit();
                }
              }}
              placeholder={t('pet.inputPlaceholder')}
              aria-label={t('pet.inputPlaceholder')}
              className="max-h-24 min-h-8 flex-1 resize-none rounded-lg border border-input bg-background px-2.5 py-1.5 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <Button type="submit" size="icon" className="size-8" disabled={props.sending || !props.draft.trim()} aria-label={t('pet.send')}>
              <ArrowUp className="size-4" />
            </Button>
          </form>
          {props.queued && <p className="px-3 pb-2 text-[11px] text-muted">{t('pet.queued')}</p>}
        </>
      ) : null}
    </section>
  );
};

const ExchangeView: React.FC<{
  exchange: ReturnType<typeof latestExchange>;
  running: boolean;
  binding: string;
}> = ({ exchange, running, binding }) => {
  const { t } = useTranslation();
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-2 text-[13px]">
      {exchange.user && (
        <div className="self-end max-w-[85%] rounded-xl bg-muted-soft px-2.5 py-1.5">
          <Markdown content={exchange.user.text} softBreaks />
        </div>
      )}
      {exchange.results.map((message) => (
        <div key={message.id} className="min-w-0">
          <Markdown content={message.text} softBreaks />
        </div>
      ))}
      {running && <p className="text-[12px] text-muted">{t('pet.state.running')}</p>}
      {!exchange.unreadComplete && (
        <Button variant="link" size="sm" className="self-start px-0" onClick={() => void petBridge.open(sessionLink(binding))}>
          {t('pet.moreInAvibe')}
        </Button>
      )}
      {!exchange.user && exchange.results.length === 0 && !running && (
        <p className="text-[12px] text-muted">{t('pet.empty')}</p>
      )}
    </div>
  );
};

const ActivityLine: React.FC<{ activities: PanelProps['activities'] }> = ({ activities }) => {
  const { t } = useTranslation();
  const sorted = useMemo(() => sortBackgroundActivities(activities), [activities]);
  if (sorted.length === 0) return null;
  const first = sorted[0];
  const label = resolveActivityLabel(first, t(`chat.activities.kind.${activityKindI18nKey(first)}`));
  return (
    <div className="px-3 pb-2">
      <StatusPill
        tone="running"
        role="status"
        className="min-h-6 max-w-full gap-2 px-2.5 py-0.5 text-[11px] font-normal"
        indicator={<Activity className="size-3 shrink-0 text-mint-ink" aria-hidden="true" />}
        label={(
          <span className="min-w-0 truncate text-muted">
            {t('chat.activities.running', { count: sorted.length })}
            {label ? ` · ${label}` : ''}
          </span>
        )}
      />
    </div>
  );
};

const NeedsInput: React.FC<{
  vaultRequestIds: string[];
  quickReplies: ReturnType<typeof openQuickReplies>;
  onChoose: (message: WorkbenchMessage, choice: string) => Promise<boolean>;
}> = ({ vaultRequestIds, quickReplies, onChoose }) => {
  const { t } = useTranslation();
  if (vaultRequestIds.length === 0 && !quickReplies) return null;
  return (
    <div className="flex flex-col gap-2 px-3 pb-2">
      {vaultRequestIds.map((requestId) => (
        <Button
          key={requestId}
          variant="outline"
          size="sm"
          className="justify-start gap-2"
          onClick={() => void petBridge.open(vaultRequestLink(requestId))}
        >
          <KeyRound className="size-3.5" />
          {t('pet.vaultRequest')}
        </Button>
      ))}
      {quickReplies && (
        <QuickReplies
          key={quickReplies.message.id}
          options={quickReplies.options}
          chosen={null}
          onChoose={(choice) => onChoose(quickReplies.message, choice)}
        />
      )}
    </div>
  );
};

const SessionSwitcher: React.FC<{ current: string | null; onPick: (sessionId: string) => void }> = ({ current, onPick }) => {
  const { t } = useTranslation();
  const { sessions, loading } = useSessionSwitcher(true);
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto py-1">
      {sessions.length === 0 && (
        <p className="px-3 py-2 text-[12px] text-muted">{loading ? t('common.loading') : t('pet.noSessions')}</p>
      )}
      {sessions.map((session) => (
        <button
          key={session.id}
          type="button"
          className={clsx(
            'truncate px-3 py-1.5 text-left text-[13px] hover:bg-muted-soft',
            session.id === current && 'font-medium text-primary-ink',
          )}
          onClick={() => onPick(session.id)}
        >
          {session.title || t('pet.untitled')}
        </button>
      ))}
    </div>
  );
};
