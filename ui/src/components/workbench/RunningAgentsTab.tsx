import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Activity,
  AlertTriangle,
  Bot,
  ExternalLink,
  Loader2,
  MessageSquare,
  Power,
  RefreshCw,
  ServerCrash,
  Square,
  Trash2,
} from 'lucide-react';
import clsx from 'clsx';
import { Link } from 'react-router-dom';

import { useApi } from '../../context/ApiContext';
import type { RunningAgent } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { PlatformIcon } from '../visual/PlatformIcon';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import {
  BACKEND_LABEL,
  BACKEND_TEXT as BACKEND_ICON_CLASS,
  type Backend,
} from '../../lib/backendAccent';

// Degraded-mode refresh cadence while SSE is disconnected.
const POLL_INTERVAL_MS = 4000;
const LIVENESS_POLL_INTERVAL_MS = 30000;
const RENDER_TICK_INTERVAL_MS = 1000;

type DisplayRunningAgent = RunningAgent & {
  elapsed_observed_at: number;
};

// Humanize elapsed_seconds: 12 → "12s", 185 → "3m", 3700 → "1h"
function formatElapsed(seconds: number | null): string {
  if (seconds == null) return '—';
  // Clamp negatives: a malformed/clock-skewed baseline must never read "-3s".
  const s = Math.max(0, seconds);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

// Truncate workdir for display — keep the last two path segments.
function shortWorkdir(workdir: string | null): string {
  if (!workdir) return '—';
  const parts = workdir.replace(/\/$/, '').split('/');
  if (parts.length <= 2) return workdir;
  return `…/${parts[parts.length - 2]}/${parts[parts.length - 1]}`;
}

// Stable identity for ordering — must NOT change between polls so rows don't
// visually jump. (Deliberately excludes elapsed_seconds, which ticks every poll.)
function identityKey(a: RunningAgent): string {
  return (
    (a.session_id ? `${a.backend}:${a.session_id}` : null) ??
    a.composite_key ??
    `${a.backend}:${a.base_session_id ?? ''}:${a.workdir ?? ''}:${a.pid ?? ''}:${a.native_session_id ?? ''}`
  );
}

// Sort: active first, then idle, then orphan; WITHIN a state band order by a
// stable identity (not elapsed) so the list stays put across polls instead of
// reshuffling as durations tick.
function sortAgents(agents: RunningAgent[]): RunningAgent[] {
  return [...agents].sort((a, b) => {
    const stateRank = (s: string) => (s === 'active' ? 0 : s === 'idle' ? 1 : 2);
    const sr = stateRank(a.state) - stateRank(b.state);
    if (sr !== 0) return sr;
    return identityKey(a).localeCompare(identityKey(b));
  });
}

function currentElapsedSeconds(agent: DisplayRunningAgent, now: number): number | null {
  if (agent.elapsed_seconds == null) return null;
  const age = Math.max(0, (now - agent.elapsed_observed_at) / 1000);
  return agent.elapsed_seconds + age;
}

// Per-state presentation, in one place: the state dot/badge AND the End button
// label/icon/confirm-policy all key off this map instead of parallel ternaries.
type RunState = 'active' | 'idle' | 'orphan';
const STATE_META: Record<
  RunState,
  {
    dotClass: string;
    badgeVariant: 'success' | 'secondary' | 'warning';
    stateKey: string;
    endKey: string;
    EndIcon: React.ComponentType<{ className?: string }>;
    needsConfirm: boolean;
  }
> = {
  active: { dotClass: 'bg-mint', badgeVariant: 'success', stateKey: 'agents.running.stateActive', endKey: 'agents.running.endActive', EndIcon: Square, needsConfirm: true },
  idle: { dotClass: 'bg-muted', badgeVariant: 'secondary', stateKey: 'agents.running.stateIdle', endKey: 'agents.running.endIdle', EndIcon: Power, needsConfirm: false },
  orphan: { dotClass: 'bg-amber-500', badgeVariant: 'warning', stateKey: 'agents.running.stateOrphan', endKey: 'agents.running.endOrphan', EndIcon: Trash2, needsConfirm: true },
};
const stateMeta = (state: string) => STATE_META[(state as RunState)] ?? STATE_META.idle;

interface RunningAgentsTabProps {
  onActiveCountChange?: (count: number | null) => void;
}

export const RunningAgentsTab: React.FC<RunningAgentsTabProps> = ({ onActiveCountChange }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [agents, setAgents] = useState<DisplayRunningAgent[]>([]);
  const [renderNow, setRenderNow] = useState(() => Date.now());
  const [unreachable, setUnreachable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [eventBridgeConnected, setEventBridgeConnected] = useState(false);
  // Guards against setState after unmount: an in-flight poll can resolve after
  // the user leaves the Running tab (the component unmounts) — without this the
  // resolved fetch would call setState on an unmounted component.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const fetchData = useCallback(
    async (isBackground = false) => {
      if (!isBackground) setLoading(true);
      try {
        const result = await api.getRunningAgents();
        if (!mountedRef.current) return;
        if (!result.ok && result.unreachable) {
          setUnreachable(true);
          setAgents([]);
          onActiveCountChange?.(null);
        } else {
          const observedAt = Date.now();
          setUnreachable(false);
          setRenderNow(observedAt);
          setAgents(sortAgents(result.agents ?? []).map((agent) => ({ ...agent, elapsed_observed_at: observedAt })));
          onActiveCountChange?.((result.counts as any)?.active ?? 0);
        }
      } catch {
        // Any unexpected fetch failure (network/401/non-JSON) means we cannot
        // trust the list — surface the explicit unreachable state (I1) rather
        // than silently leaving stale rows with no indication.
        if (mountedRef.current) {
          setUnreachable(true);
          setAgents([]);
          onActiveCountChange?.(null);
        }
      } finally {
        if (mountedRef.current && !isBackground) setLoading(false);
      }
    },
    [api, onActiveCountChange],
  );

  // End a running agent's live runtime, dispatched controller-side by
  // backend+state (active→interrupt+disconnect, idle→disconnect, orphan→kill
  // process). Best-effort: refresh right after so the row reflects the change
  // (the next poll reconciles authoritatively anyway).
  const endAgent = useCallback(
    async (agent: RunningAgent) => {
      let result: Awaited<ReturnType<typeof api.endRunningAgent>> | undefined;
      try {
        result = await api.endRunningAgent({
          backend: agent.backend,
          state: agent.state,
          session_id: agent.session_id,
          composite_key: agent.composite_key,
          base_session_id: agent.base_session_id,
          pid: agent.pid,
        });
      } catch (err) {
        // A network failure shouldn't bubble into an unhandled rejection.
        console.warn('running-agents: end failed', err);
      }
      // Always give the user feedback — silence reads as "nothing happened".
      if (result?.ok) {
        showToast(t('agents.running.endedToast'), 'success');
      } else {
        const reason = result?.error || (result?.unreachable ? 'unreachable' : 'failed');
        showToast(t('agents.running.endFailedToast', { error: reason }), 'error');
      }
      if (mountedRef.current) await fetchData(true);
    },
    [api, fetchData, showToast, t],
  );

  useEffect(() => {
    fetchData(false);
  }, [fetchData]);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      onConnected: (data) => {
        if (data.source === 'controller') {
          setEventBridgeConnected(true);
          fetchData(true);
        }
      },
      onEventBridgeStatus: ({ connected }) => {
        setEventBridgeConnected(connected);
        if (connected) fetchData(true);
      },
      onError: () => setEventBridgeConnected(false),
      onRunsUpdated: () => fetchData(true),
      onTurnStart: () => fetchData(true),
      onTurnEnd: () => fetchData(true),
      onSessionStatus: () => fetchData(true),
    });
  }, [api, fetchData]);

  const hasElapsedRows = agents.some((agent) => agent.elapsed_seconds != null);
  useEffect(() => {
    if (!hasElapsedRows) return;
    const timer = window.setInterval(() => setRenderNow(Date.now()), RENDER_TICK_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [hasElapsedRows]);

  // SSE covers lifecycle writes, and the render ticker covers elapsed labels.
  // External process death/orphan detection is a sampled snapshot, so keep a
  // low-rate reconciliation poll only while visible rows can go stale.
  useEffect(() => {
    const intervalMs = eventBridgeConnected ? LIVENESS_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
    if (eventBridgeConnected && agents.length === 0) return;
    let timer: number | undefined;
    let cancelled = false;
    let inFlight = false;
    let pendingWake = false;

    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState !== 'visible') {
        timer = window.setTimeout(tick, intervalMs);
        return;
      }
      if (inFlight) {
        pendingWake = true;
        return;
      }
      inFlight = true;
      window.clearTimeout(timer);
      try {
        await fetchData(true);
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      if (pendingWake) {
        pendingWake = false;
        void tick();
        return;
      }
      timer = window.setTimeout(tick, intervalMs);
    };

    const refreshNow = () => {
      if (document.visibilityState === 'visible') void tick();
    };

    void tick();
    document.addEventListener('visibilitychange', refreshNow);
    window.addEventListener('focus', refreshNow);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', refreshNow);
      window.removeEventListener('focus', refreshNow);
    };
  }, [agents.length, eventBridgeConnected, fetchData]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="size-5 animate-spin text-muted" />
      </div>
    );
  }

  if (unreachable) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-amber/40 bg-amber/[0.04] px-6 py-12 text-center">
        <ServerCrash className="size-8 text-amber-500" />
        <div className="text-[14px] font-semibold text-foreground">
          {t('agents.running.unreachableTitle')}
        </div>
        <div className="max-w-sm text-[12px] text-muted">{t('agents.running.unreachableBody')}</div>
        <Button
          type="button"
          variant="outline"
          size="xs"
          onClick={() => fetchData(false)}
          className="mt-2"
        >
          <RefreshCw className="size-3.5" />
          {t('common.refresh')}
        </Button>
      </div>
    );
  }

  if (agents.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center">
        <Activity className="size-8 text-muted" />
        <div className="text-[13px] text-muted">{t('agents.running.empty')}</div>
        <Button
          type="button"
          variant="outline"
          size="xs"
          onClick={() => fetchData(false)}
          className="mt-1"
        >
          <RefreshCw className="size-3.5" />
          {t('common.refresh')}
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Header row with manual refresh */}
      <div className="flex items-center justify-end">
        <Button type="button" variant="outline" size="xs" onClick={() => fetchData(false)}>
          <RefreshCw className="size-3.5" />
          {t('common.refresh')}
        </Button>
      </div>

      {agents.map((agent) => (
        <RunningAgentRow
          // Stable identity (survives re-sorts so React doesn't remount rows) —
          // same key the sort uses; no positional index.
          key={identityKey(agent)}
          agent={agent}
          renderNow={renderNow}
          onEnd={endAgent}
        />
      ))}
    </div>
  );
};

// ---------------------------------------------------------------------------
// One row / card per running agent session
// ---------------------------------------------------------------------------

interface RunningAgentRowProps {
  agent: DisplayRunningAgent;
  renderNow: number;
  onEnd: (agent: RunningAgent) => Promise<void>;
}

const RunningAgentRow: React.FC<RunningAgentRowProps> = ({ agent, renderNow, onEnd }) => {
  const { t } = useTranslation();
  const isOrphan = agent.state === 'orphan';
  const [ending, setEnding] = useState(false);
  // Two-step confirm for destructive ends (active loses in-flight work; orphan
  // sends SIGTERM/SIGKILL). Idle disconnect is harmless (session is re-creatable)
  // so it ends on the first click.
  const [armed, setArmed] = useState(false);
  const rowMounted = useRef(true);
  const disarmTimer = useRef<number | null>(null);
  useEffect(() => () => {
    rowMounted.current = false;
    if (disarmTimer.current != null) window.clearTimeout(disarmTimer.current);
  }, []);

  // Per-state End presentation (Stop / Disconnect / Kill) + confirm policy.
  const meta = stateMeta(agent.state);
  const needsConfirm = meta.needsConfirm;
  const endLabel = t(meta.endKey);
  const EndIcon = meta.EndIcon;

  const runEnd = async () => {
    setArmed(false);
    if (disarmTimer.current != null) window.clearTimeout(disarmTimer.current);
    setEnding(true);
    try {
      await onEnd(agent);
    } finally {
      if (rowMounted.current) setEnding(false);
    }
  };

  const handleEndClick = () => {
    if (ending) return;
    if (needsConfirm && !armed) {
      setArmed(true);
      if (disarmTimer.current != null) window.clearTimeout(disarmTimer.current);
      disarmTimer.current = window.setTimeout(() => {
        if (rowMounted.current) setArmed(false);
      }, 3000);
      return;
    }
    void runEnd();
  };

  const backendLabel =
    BACKEND_LABEL[agent.backend as Backend] ?? agent.backend;
  const backendClass =
    BACKEND_ICON_CLASS[agent.backend as Backend] ?? 'text-muted';
  const elapsedSeconds = currentElapsedSeconds(agent, renderNow);

  const canOpenChat = agent.openable_in_chat && !!agent.session_id;

  return (
    <div
      className={clsx(
        'flex min-w-0 flex-col gap-2 rounded-xl border px-4 py-3 transition',
        isOrphan
          ? 'border-amber/40 bg-amber/[0.04]'
          : 'border-border bg-surface',
      )}
    >
      {/* ── Row top: backend · state dot · elapsed · platform · chat link ── */}
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {/* Backend badge */}
        <span className={clsx('font-mono text-[11px] font-bold uppercase tracking-wide', backendClass)}>
          {backendLabel}
        </span>

        {/* State indicator */}
        <StateDot state={agent.state} t={t} />

        {/* Elapsed — "busy {{elapsed}}" while a turn is in flight (the backend
            anchors this to the turn-start baseline, not last-chunk time), else
            "idle {{elapsed}}". */}
        {elapsedSeconds != null && (
          <span className="font-mono text-[11px] text-muted">
            {t(
              agent.state === 'active'
                ? 'agents.running.busy'
                : 'agents.running.idleElapsed',
              { elapsed: formatElapsed(elapsedSeconds) },
            )}
          </span>
        )}

        {/* Orphan label */}
        {isOrphan && (
          <Badge variant="warning" className="font-mono text-[9px] uppercase">
            <AlertTriangle className="size-2.5" />
            {t('agents.running.orphan')}
          </Badge>
        )}

        {/* Spacer */}
        <div className="flex-1" />

        {/* Chat link — IM (slack/discord/…) sessions included; when a row has no
            openable session we render nothing at all (no "unavailable" label). */}
        {canOpenChat && (
          <Link
            to={`/chat/${encodeURIComponent(agent.session_id ?? '')}`}
            className="inline-flex shrink-0 items-center gap-1 text-[11px] font-medium text-cyan hover:underline"
          >
            <MessageSquare className="size-3" />
            {t('agents.running.openChat')}
            <ExternalLink className="size-2.5" />
          </Link>
        )}

        {/* Unified End — terminates this agent's live runtime, dispatched by
            state: Stop (active) / Disconnect (idle) / Kill process (orphan).
            Destructive ends (active/orphan) require a 2nd confirming click. */}
        <Button
          type="button"
          variant={armed ? 'destructive' : 'destructive-soft'}
          size={armed ? 'xs' : 'icon'}
          onClick={handleEndClick}
          disabled={ending}
          aria-label={armed ? t('agents.running.confirmEnd') : endLabel}
          title={armed ? t('agents.running.confirmEnd') : endLabel}
          className={clsx('shrink-0', armed ? 'h-7' : 'size-7')}
        >
          {ending ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : armed ? (
            t('agents.running.confirmEnd')
          ) : (
            <EndIcon className="size-3.5" />
          )}
        </Button>
      </div>

      {/* ── Row bottom: title · platform/scope · workdir · model · pid ── */}
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
        {/* Title */}
        {agent.title && (
          <span className="min-w-0 max-w-[200px] truncate font-medium text-foreground" title={agent.title}>
            {agent.title}
          </span>
        )}

        {/* Origin: agent-initiated private runs have no IM platform — label them
            explicitly as "agent" + the run name; otherwise show platform/scope. */}
        {agent.trigger_source === 'agent' ? (
          <span className="inline-flex shrink-0 items-center gap-1 text-violet">
            <Bot className="size-3" />
            <span className="uppercase tracking-wide">{t('agents.running.agentInitiated')}</span>
            {agent.scope_display_name && (
              <span className="max-w-[140px] truncate text-muted" title={agent.scope_display_name}>
                · {agent.scope_display_name}
              </span>
            )}
          </span>
        ) : agent.platform ? (
          <span className="inline-flex shrink-0 items-center gap-1">
            <PlatformIcon platform={agent.platform as any} size={12} />
            <span className="uppercase tracking-wide">{agent.platform}</span>
            {agent.scope_type && <span>·</span>}
            {agent.scope_type && <span>{agent.scope_type}</span>}
            {agent.scope_display_name && (
              <span className="max-w-[120px] truncate" title={agent.scope_display_name}>
                · {agent.scope_display_name}
              </span>
            )}
          </span>
        ) : null}

        {/* Workdir */}
        {agent.workdir && (
          <span
            className="max-w-[180px] truncate font-mono text-[10px]"
            title={agent.workdir}
          >
            {shortWorkdir(agent.workdir)}
          </span>
        )}

        {/* Model */}
        {agent.model && (
          <span className="max-w-[160px] shrink-0 truncate font-mono text-[10px]" title={agent.model}>
            {agent.model}
          </span>
        )}

        {/* PID — omit for opencode, omit if null */}
        {agent.backend !== 'opencode' && typeof agent.pid === 'number' && (
          <span className="shrink-0 font-mono text-[10px]">
            pid {agent.pid}
            {agent.pid_shared && (
              <span className="ml-1 text-amber-500">{t('agents.running.pidShared')}</span>
            )}
          </span>
        )}

        {/* Agent name */}
        {agent.agent_name && (
          <span className="shrink-0 text-[10px]">{agent.agent_name}</span>
        )}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// State dot: colored circle + label
// ---------------------------------------------------------------------------

const StateDot: React.FC<{ state: string; t: (k: string) => string }> = ({ state, t }) => {
  const meta = stateMeta(state);
  return (
    <Badge variant={meta.badgeVariant} className="font-mono text-[9px] uppercase">
      <span className={clsx('size-1.5 rounded-full', meta.dotClass)} />
      {t(meta.stateKey)}
    </Badge>
  );
};
