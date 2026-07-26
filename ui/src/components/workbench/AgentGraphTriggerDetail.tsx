import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { CalendarClock, ChevronRight, ExternalLink, Eye, Loader2, Power, X } from 'lucide-react';
import clsx from 'clsx';

import { ApiError, useApi } from '../../context/ApiContext';
import type { HarnessRun, HarnessTask, HarnessWatch } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { Button } from '../ui/button';
import { Switch } from '../ui/switch';
import { formatLocalDateTime } from '../../lib/datetime';
import { formatRelativeTime } from '../../lib/relativeTime';
import {
  type AgentGraphEdge,
  type AgentGraphNode,
  type AgentGraphStatus,
  type AgentGraphTriggerNode,
  nodeDisplayTitle,
  statusMeta,
  triggerFiredSessionIds,
} from '../../lib/agentGraph';

// While a trigger detail panel is open, re-fetch its definition AND recent runs
// on this cadence so a watch's runtime (running/pid) tracks WatchSupervisor's
// async worker start/stop even without a local toggle, newly-fired runs appear
// without a reopen, and a transient list-fetch failure recovers on its own.
// Background ticks are silent (no per-tick error toast). Matches the tab's
// degraded-mode poll.
const TRIGGER_DETAIL_POLL_MS = 4000;

// Definition load lifecycle: loading → ready (row found) | absent (fetch OK but
// row gone) | error (fetch threw — transient, the poll retries). 'absent'
// disables the switch; 'error' does not.
type DefState = 'loading' | 'ready' | 'absent' | 'error';

interface AgentGraphTriggerDetailProps {
  trigger: AgentGraphTriggerNode;
  // Raw graph edges (not the disabled-filtered set) so the fired-session list is
  // complete even while the chip itself is hidden by the legend toggle.
  edges: AgentGraphEdge[];
  nodesById: Map<string, AgentGraphNode>;
  onClose: () => void;
  onSelectNode: (sessionId: string) => void;
  // Refetch the graph after an enable/disable so the chip re-dims / re-hides.
  onRefresh: () => void;
}

// A11: clicking a Task/Watch chip opens this in-graph panel (same interaction as
// a session node) instead of navigating to Harness. It surfaces the definition's
// schedule/next-run (task) or command/runtime (watch), the sessions it fired,
// recent trigger runs, and an in-place enable/disable toggle — a watch's toggle
// IS its pause/resume. Everything reuses the existing harness API client methods;
// "Open in Harness" survives as a secondary exit.
export const AgentGraphTriggerDetail: React.FC<AgentGraphTriggerDetailProps> = ({
  trigger,
  edges,
  nodesById,
  onClose,
  onSelectNode,
  onRefresh,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const isWatch = trigger.definition_type === 'watch';
  const name = trigger.name?.trim() || trigger.definition_id;

  // Full definition (for next-run / command / runtime). There is no single-item
  // GET, so the unparameterized list endpoint — which returns the complete set —
  // is the sanctioned source; we find the row by id. No new backend (A11).
  const [task, setTask] = useState<HarnessTask | null>(null);
  const [watch, setWatch] = useState<HarnessWatch | null>(null);
  const [defState, setDefState] = useState<DefState>('loading');
  const [runs, setRuns] = useState<HarnessRun[]>([]);
  // Enabled is optimistic-local so the switch reacts instantly; seeded from the
  // chip, then reconciled to the fetched definition / PATCH response.
  const [enabled, setEnabled] = useState(trigger.enabled);
  const [busy, setBusy] = useState(false);

  const definitionId = trigger.definition_id;
  const chipEnabled = trigger.enabled;
  // The definition this panel currently shows, mirrored into a ref so an async
  // fetch/toggle can tell — after its await — whether the user has since selected
  // a different chip. The panel instance is reused across selections, so a late
  // resolution for a no-longer-active definition must not write state here.
  const activeDefIdRef = useRef(definitionId);
  activeDefIdRef.current = definitionId;
  // Mirror `busy` into a ref so the freshness poll can skip a tick while a toggle
  // is in flight without resubscribing the interval on every busy change.
  const busyRef = useRef(busy);
  busyRef.current = busy;

  // Derived, so 'error' (transient) never disables the switch the way a genuine
  // 'absent' does — a flaky list request must not leave a real trigger inert.
  const defPending = defState === 'loading' || defState === 'error';
  const defMissing = defState === 'absent';

  // Load the definition + recent runs on selection, then keep both fresh while
  // the panel stays open. There is no single-item GET, so the full list endpoint
  // is the source (no new backend). One steady path serves four needs:
  //  • first load of name/schedule/command + enabled state;
  //  • a watch's runtime (running/pid) tracks WatchSupervisor's async worker
  //    start/stop even when nothing is toggled locally;
  //  • recent runs pick up a trigger that fires while the panel stays open;
  //  • a transient list-fetch failure recovers on the next tick instead of
  //    leaving the trigger permanently non-actionable.
  // Definition ticks are skipped while a toggle is in flight so the poll can't
  // clobber the optimistic switch or the PATCH's authoritative response, and
  // background ticks are silent so a down endpoint can't toast every 4s.
  useEffect(() => {
    setTask(null);
    setWatch(null);
    setDefState('loading');
    setEnabled(chipEnabled);
    // A new chip must not inherit the previous definition's rows or an in-flight
    // toggle's busy/optimistic state (that toggle's late resolution is separately
    // dropped via activeDefIdRef). `setBusy(false)` only takes effect on the next
    // render, so also clear the ref *synchronously* — otherwise this run's own
    // immediate loadDefinition() would still read the previous trigger's
    // busyRef===true and bail, stranding the new chip in `loading` (switch
    // disabled) until the 4s interval fires. B has no in-flight toggle of its own,
    // so an unconditional clear here is correct; the previous toggle's late writes
    // stay gated by activeDefIdRef regardless of this ref.
    setRuns([]);
    setBusy(false);
    busyRef.current = false;

    let stopped = false;
    // `silent` (background ticks) suppresses the API client's global error toast
    // so a persistently-unavailable endpoint doesn't toast every 4s; the first
    // foreground load still surfaces one toast. The panel's own '…' / disabled
    // state is the durable error surface either way.
    const loadDefinition = async (silent: boolean) => {
      if (stopped || busyRef.current || activeDefIdRef.current !== definitionId) return;
      const opts = silent ? { handleError: false } : undefined;
      try {
        if (isWatch) {
          const res = await api.listHarnessWatches(undefined, opts);
          if (stopped || busyRef.current || activeDefIdRef.current !== definitionId) return;
          const found = res.watches.find((w) => w.id === definitionId);
          if (found) {
            setWatch(found);
            setEnabled(found.enabled);
            setDefState('ready');
          } else {
            setDefState('absent');
          }
        } else {
          const res = await api.listHarnessTasks(undefined, opts);
          if (stopped || busyRef.current || activeDefIdRef.current !== definitionId) return;
          const found = res.tasks.find((tk) => tk.id === definitionId);
          if (found) {
            setTask(found);
            setEnabled(found.enabled);
            setDefState('ready');
          } else {
            setDefState('absent');
          }
        }
      } catch {
        // Keep any last-known row; only flag a transient error when there is
        // nothing to show yet. The next tick retries either way.
        if (!stopped && activeDefIdRef.current === definitionId) {
          setDefState((s) => (s === 'ready' ? s : 'error'));
        }
      }
    };

    // Recent trigger runs — refreshed on the SAME cadence so a run that fires
    // while the panel stays open appears without a reopen (an SSE graph refresh
    // doesn't reload these panel-local rows). Always silent: a runs failure has a
    // self-evident empty state (the section just doesn't render) and needs no
    // toast. Not gated on `busy` — runs are orthogonal to the enable toggle.
    const loadRuns = async () => {
      if (stopped || activeDefIdRef.current !== definitionId) return;
      try {
        const res = await api.listHarnessRuns({ definitionId, limit: 6 }, { handleError: false });
        if (!stopped && activeDefIdRef.current === definitionId) setRuns(res.runs ?? []);
      } catch {
        // Keep the last-known rows; the next tick retries.
      }
    };

    void loadDefinition(false);
    void loadRuns();
    const timer = window.setInterval(() => {
      void loadDefinition(true);
      void loadRuns();
    }, TRIGGER_DETAIL_POLL_MS);

    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [api, definitionId, isWatch, chipEnabled]);

  // Sessions this definition fired in-window (newest first), restricted to nodes
  // actually present in the graph so every row selects a real node.
  const firedSessions = useMemo(
    () => triggerFiredSessionIds(definitionId, edges).filter((id) => nodesById.has(id)),
    [definitionId, edges, nodesById],
  );

  const scheduleText = trigger.schedule_label?.trim() || task?.cron || task?.run_at || task?.schedule_type || '—';
  const commandText =
    watch?.shell_command?.trim() ||
    (Array.isArray(watch?.command) ? (watch?.command as unknown[]).join(' ') : '') ||
    '—';
  const runtimeText = watch?.runtime?.running
    ? watch.runtime.pid != null
      ? t('agents.graph.triggerDetail.runningPid', { pid: watch.runtime.pid })
      : t('agents.graph.triggerDetail.running')
    : t('agents.graph.triggerDetail.notRunning');

  const toggleEnabled = async (next: boolean) => {
    // Pin the definition this toggle acts on; if the user switches chips before
    // the PATCH resolves, the panel-local writes below bail so a stale response
    // can't overwrite the now-foreign definition's state in this reused panel.
    const callId = definitionId;
    const stillActive = () => activeDefIdRef.current === callId;
    setBusy(true);
    const prev = enabled;
    setEnabled(next); // optimistic
    try {
      if (isWatch) {
        const res = await api.setHarnessWatchEnabled(callId, next);
        if (!res.ok) throw new Error('toggle rejected');
        if (stillActive() && res.watch) {
          setWatch(res.watch);
          setEnabled(res.watch.enabled);
        }
      } else {
        const res = await api.setHarnessTaskEnabled(callId, next);
        if (!res.ok) throw new Error('toggle rejected');
        if (stillActive() && res.task) {
          setTask(res.task);
          setEnabled(res.task.enabled);
        }
      }
      // The change is committed server-side even if the user has since selected
      // another chip, so refresh the graph unconditionally — the toggled chip
      // must re-dim / re-hide now, not only after the next background poll.
      onRefresh();
      if (stillActive()) {
        showToast(
          t(next ? 'agents.graph.triggerDetail.enabledToast' : 'agents.graph.triggerDetail.disabledToast'),
          'success',
        );
        // No bespoke runtime chase here: the open-panel poll picks up the watch's
        // settled running/pid once WatchSupervisor's async worker start/stop lands.
      }
    } catch (err) {
      // Revert + notify only while still viewing this definition. An HTTP error
      // was already surfaced by the API client's global error toast, so only the
      // soft ``{ ok: false }`` / network path (a non-ApiError throw) notifies
      // here — one failure never yields two toasts.
      if (stillActive()) {
        setEnabled(prev);
        if (!(err instanceof ApiError)) {
          showToast(t('agents.graph.triggerDetail.toggleFailed'), 'error');
        }
      }
    } finally {
      if (stillActive()) setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3.5">
      {/* Header: type + enabled pills + close */}
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-violet/40 bg-violet-soft px-2.5 py-0.5 text-[11px] font-semibold text-violet">
          {isWatch ? <Eye className="size-3" /> : <CalendarClock className="size-3" />}
          {t(isWatch ? 'agents.graph.triggerDetail.watchType' : 'agents.graph.triggerDetail.taskType')}
        </span>
        <span
          className={clsx(
            'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold',
            enabled
              ? 'border-mint/40 bg-mint-soft text-mint'
              : 'border-border-strong bg-foreground/[0.04] text-muted',
          )}
        >
          {t(enabled ? 'agents.graph.triggerDetail.enabled' : 'agents.graph.triggerDetail.disabled')}
        </span>
        <span className="flex-1" />
        <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label={t('common.close')} className="size-6">
          <X className="size-3.5" />
        </Button>
      </div>

      {/* Title + id */}
      <div className="flex flex-col gap-1">
        <div className="break-words text-[16px] font-bold text-foreground">{name}</div>
        <div className="font-mono text-[10px] text-muted">{trigger.definition_id}</div>
      </div>

      <div className="h-px bg-border" />

      {/* Facts: schedule/next-run (task) · command/runtime (watch) */}
      <div className="flex flex-col gap-2.5">
        {isWatch ? (
          <>
            <Fact label={t('agents.graph.triggerDetail.command')}>
              <span className="break-all font-mono text-[11px]">{defPending && !watch ? '…' : commandText}</span>
            </Fact>
            <Fact label={t('agents.graph.triggerDetail.runtime')}>
              <span className="inline-flex items-center gap-1.5">
                <span
                  className={clsx(
                    'size-1.5 rounded-full',
                    watch?.runtime?.running ? 'bg-mint' : 'bg-muted',
                  )}
                />
                {defPending && !watch ? '…' : runtimeText}
              </span>
            </Fact>
          </>
        ) : (
          <>
            <Fact label={t('agents.graph.triggerDetail.schedule')}>
              <span className="font-mono text-[11px]">{scheduleText}</span>
            </Fact>
            <Fact label={t('agents.graph.triggerDetail.nextRun')}>
              <span className="font-mono text-[11px]">
                {defPending && !task ? '…' : task?.next_run_at ? formatLocalDateTime(task.next_run_at) : '—'}
              </span>
            </Fact>
          </>
        )}
      </div>

      {/* Triggered sessions — each selects that node on the graph */}
      <div className="flex flex-col gap-1.5">
        <SectionLabel>{t('agents.graph.triggerDetail.firedSessions')}</SectionLabel>
        {firedSessions.length > 0 ? (
          <div className="flex flex-col gap-1">
            {firedSessions.map((id) => {
              const node = nodesById.get(id)!;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => onSelectNode(id)}
                  className="flex items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-left text-[11px] transition hover:border-border-strong"
                >
                  <span className={clsx('size-1.5 shrink-0 rounded-full', statusMeta(node.status).dotClass)} />
                  <span className="min-w-0 flex-1 truncate text-foreground">{nodeDisplayTitle(node)}</span>
                  <ChevronRight className="size-3.5 shrink-0 text-muted" />
                </button>
              );
            })}
          </div>
        ) : (
          <span className="text-[11px] text-muted">{t('agents.graph.triggerDetail.noFiredSessions')}</span>
        )}
      </div>

      {/* Recent trigger runs (deep-linked into Harness) */}
      {runs.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <SectionLabel>{t('agents.graph.triggerDetail.runsTitle')}</SectionLabel>
            <Link to="/harness?tab=runs" className="text-[11px] font-medium text-cyan hover:underline">
              {t('agents.graph.detail.viewAllInHarness')}
            </Link>
          </div>
          <div className="flex flex-col gap-1">
            {runs.map((run) => (
              <Link
                key={run.id}
                to={`/harness?tab=runs&run=${encodeURIComponent(run.id)}`}
                className="flex items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[11px] transition hover:border-border-strong"
              >
                <span className={clsx('size-1.5 shrink-0 rounded-full', statusMeta(runStatus(run.status)).dotClass)} />
                <code className="font-mono text-foreground">{run.id}</code>
                <span className="text-muted">{t(statusMeta(runStatus(run.status)).labelKey)}</span>
                <span className="flex-1" />
                <span className="font-mono text-[10px] text-muted">{formatRelativeTime(run.created_at, t)}</span>
              </Link>
            ))}
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex flex-col gap-2 pt-1">
        <div className="flex items-center justify-between rounded-lg border border-border bg-surface px-3 py-2">
          <span className="inline-flex items-center gap-2 text-[12px] font-medium text-foreground">
            <Power className="size-3.5 text-muted" />
            {t(isWatch ? 'agents.graph.triggerDetail.watchActiveLabel' : 'agents.graph.triggerDetail.taskActiveLabel')}
          </span>
          <span className="inline-flex items-center gap-2">
            {busy && <Loader2 className="size-3 animate-spin text-muted" />}
            <Switch
              checked={enabled}
              onCheckedChange={toggleEnabled}
              // Also gate on defPending: a toggle fired before the first load
              // settles could otherwise race the in-flight list GET, whose late
              // setEnabled(found.enabled) would clobber the PATCH result. Because
              // a transient error AFTER a successful load keeps state 'ready'
              // (never 'error'), defPending only means "never loaded yet" — so
              // this never re-disables a real trigger on a flaky refresh.
              disabled={busy || defMissing || defPending}
              label={t(isWatch ? 'agents.graph.triggerDetail.watchActiveLabel' : 'agents.graph.triggerDetail.taskActiveLabel')}
            />
          </span>
        </div>
        <Link
          to={`/harness?tab=${isWatch ? 'watches' : 'tasks'}`}
          className="inline-flex items-center justify-center gap-1.5 text-[11px] font-medium text-muted transition hover:text-foreground"
        >
          <ExternalLink className="size-3" />
          {t('agents.graph.triggerDetail.openInHarness')}
        </Link>
      </div>
    </div>
  );
};

// A run row's stored status uses the run vocabulary; map it onto the node status
// vocabulary the shared statusMeta understands (a live run's ``running`` reuses
// the ``active`` dot). Mirrors AgentGraphDetail.runStatus.
function runStatus(status: string): AgentGraphStatus {
  if (status === 'running') return 'active';
  const known: AgentGraphStatus[] = ['queued', 'succeeded', 'failed', 'canceled', 'idle', 'active', 'orphan'];
  return (known as string[]).includes(status) ? (status as AgentGraphStatus) : 'idle';
}

const SectionLabel: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{children}</span>
);

const Fact: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="flex items-start gap-3">
    <span className="w-16 shrink-0 pt-0.5 font-mono text-[10px] font-bold uppercase tracking-wide text-muted">{label}</span>
    <div className="min-w-0 flex-1 text-[12px] text-foreground">{children}</div>
  </div>
);
