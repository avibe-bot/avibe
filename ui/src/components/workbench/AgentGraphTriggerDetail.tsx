import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { CalendarClock, ChevronRight, ExternalLink, Eye, Loader2, Power, X } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
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
  const [defLoading, setDefLoading] = useState(true);
  const [defMissing, setDefMissing] = useState(false);
  const [runs, setRuns] = useState<HarnessRun[]>([]);
  // Enabled is optimistic-local so the switch reacts instantly; seeded from the
  // chip, then reconciled to the fetched definition / PATCH response.
  const [enabled, setEnabled] = useState(trigger.enabled);
  const [busy, setBusy] = useState(false);

  const definitionId = trigger.definition_id;
  const chipEnabled = trigger.enabled;
  // The definition this panel currently shows, mirrored into a ref so an async
  // toggle can tell — after its await — whether the user has since selected a
  // different chip. The panel instance is reused across selections, so a late
  // PATCH resolution for a no-longer-active definition must not write state here.
  const activeDefIdRef = useRef(definitionId);
  activeDefIdRef.current = definitionId;

  useEffect(() => {
    let cancelled = false;
    setTask(null);
    setWatch(null);
    setDefLoading(true);
    setDefMissing(false);
    setEnabled(chipEnabled);
    // Selecting another chip must not inherit the previous definition's rows or
    // an in-flight toggle's busy/optimistic state (that toggle's late resolution
    // is separately dropped via activeDefIdRef).
    setRuns([]);
    setBusy(false);

    void (async () => {
      try {
        if (isWatch) {
          const res = await api.listHarnessWatches();
          const found = res.watches.find((w) => w.id === definitionId) ?? null;
          if (cancelled) return;
          setWatch(found);
          if (found) setEnabled(found.enabled);
          else setDefMissing(true);
        } else {
          const res = await api.listHarnessTasks();
          const found = res.tasks.find((tk) => tk.id === definitionId) ?? null;
          if (cancelled) return;
          setTask(found);
          if (found) setEnabled(found.enabled);
          else setDefMissing(true);
        }
      } catch {
        if (!cancelled) setDefMissing(true);
      } finally {
        if (!cancelled) setDefLoading(false);
      }
    })();

    // Recent trigger runs for this definition — independent of the def fetch.
    let runsCancelled = false;
    api
      .listHarnessRuns({ definitionId, limit: 6 })
      .then((res) => {
        if (!runsCancelled) setRuns(res.runs ?? []);
      })
      .catch(() => {});

    return () => {
      cancelled = true;
      runsCancelled = true;
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
    // the PATCH resolves, every branch below bails so a stale response can't
    // write the now-foreign definition's state into this reused panel.
    const callId = definitionId;
    setBusy(true);
    const prev = enabled;
    setEnabled(next); // optimistic
    try {
      if (isWatch) {
        const res = await api.setHarnessWatchEnabled(callId, next);
        if (activeDefIdRef.current !== callId) return;
        if (!res.ok) throw new Error('toggle rejected');
        if (res.watch) {
          setWatch(res.watch);
          setEnabled(res.watch.enabled);
        }
      } else {
        const res = await api.setHarnessTaskEnabled(callId, next);
        if (activeDefIdRef.current !== callId) return;
        if (!res.ok) throw new Error('toggle rejected');
        if (res.task) {
          setTask(res.task);
          setEnabled(res.task.enabled);
        }
      }
      showToast(
        t(next ? 'agents.graph.triggerDetail.enabledToast' : 'agents.graph.triggerDetail.disabledToast'),
        'success',
      );
      onRefresh();
    } catch {
      // Only revert + notify while still viewing this definition — a switch has
      // already reset enabled/busy for the newly-selected one. The message is a
      // localized fallback, never the raw thrown control-flow string.
      if (activeDefIdRef.current === callId) {
        setEnabled(prev);
        showToast(t('agents.graph.triggerDetail.toggleFailed'), 'error');
      }
    } finally {
      if (activeDefIdRef.current === callId) setBusy(false);
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
              <span className="break-all font-mono text-[11px]">{defLoading && !watch ? '…' : commandText}</span>
            </Fact>
            <Fact label={t('agents.graph.triggerDetail.runtime')}>
              <span className="inline-flex items-center gap-1.5">
                <span
                  className={clsx(
                    'size-1.5 rounded-full',
                    watch?.runtime?.running ? 'bg-mint' : 'bg-muted',
                  )}
                />
                {defLoading && !watch ? '…' : runtimeText}
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
                {defLoading && !task ? '…' : task?.next_run_at ? formatLocalDateTime(task.next_run_at) : '—'}
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
              disabled={busy || defMissing}
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
