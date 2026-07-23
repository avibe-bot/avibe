import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import {
  Bot,
  CornerDownRight,
  CornerLeftUp,
  Eye,
  EyeOff,
  FolderClosed,
  Loader2,
  MessageSquare,
  Power,
  Square,
  Trash2,
  X,
} from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { Button } from '../ui/button';
import { formatRelativeTime } from '../../lib/relativeTime';
import {
  type AgentGraphEdge,
  type AgentGraphNode,
  type AgentGraphStatus,
  type AgentGraphTriggerNode,
  deriveLineage,
  formatElapsed,
  isBackground,
  nodeDisplayTitle,
  runElapsedSeconds,
  statusMeta,
} from '../../lib/agentGraph';

interface AgentGraphDetailProps {
  node: AgentGraphNode;
  nodesById: Map<string, AgentGraphNode>;
  edges: AgentGraphEdge[];
  triggersById: Map<string, AgentGraphTriggerNode>;
  onClose: () => void;
  onSelectNode: (sessionId: string) => void;
  onRefresh: () => void;
}

export const AgentGraphDetail: React.FC<AgentGraphDetailProps> = ({
  node,
  nodesById,
  edges,
  triggersById,
  onClose,
  onSelectNode,
  onRefresh,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [busy, setBusy] = useState(false);
  const [armEnd, setArmEnd] = useState(false);
  const disarmRef = useRef<number | null>(null);
  const disarm = () => {
    if (disarmRef.current != null) window.clearTimeout(disarmRef.current);
    disarmRef.current = null;
    setArmEnd(false);
  };
  const arm = () => {
    if (disarmRef.current != null) window.clearTimeout(disarmRef.current);
    setArmEnd(true);
    // Auto-disarm after a few seconds: the Stop/Kill button can keep focus, so
    // blur alone would leave a destructive action armed indefinitely and a later
    // stray click could terminate an active/orphan run.
    disarmRef.current = window.setTimeout(() => {
      disarmRef.current = null;
      setArmEnd(false);
    }, 3000);
  };
  // The panel stays mounted across node switches (only the prop changes), so
  // clear a pending destructive-confirm when the selected session changes — a
  // click on the new node must not skip its first confirmation — and never let
  // the disarm timer outlive the panel.
  useEffect(() => {
    setArmEnd(false);
    return () => {
      if (disarmRef.current != null) window.clearTimeout(disarmRef.current);
      disarmRef.current = null;
    };
  }, [node.session_id]);

  const lineage = deriveLineage(node.session_id, edges, triggersById);
  const meta = statusMeta(node.status);
  const background = isBackground(node);
  const timeLabel = node.live
    ? formatElapsed(node.elapsed_seconds)
    : formatRelativeTime(node.last_active_at ?? node.created_at, t);

  const callerTitle = (sessionId: string): string => {
    const caller = nodesById.get(sessionId);
    return caller ? nodeDisplayTitle(caller) : sessionId;
  };

  // 触发 (trigger source): a Task/Watch definition, else an agent-initiated run,
  // else the human/platform origin.
  const triggerLabel = lineage.trigger
    ? `${lineage.trigger.definition_type === 'watch' ? t('agents.graph.trigger.watch') : t('agents.graph.trigger.task')} · ${lineage.trigger.name ?? lineage.trigger.definition_id}`
    : lineage.spawnedBy
      ? t('agents.graph.detail.triggerAgentRun')
      : t('agents.graph.detail.triggerHuman');

  const toggleVisibility = async () => {
    if (!node.visibility) return;
    const next = node.visibility === 'foreground' ? 'background' : 'foreground';
    setBusy(true);
    try {
      await api.setSessionVisibility(node.session_id, next);
      showToast(t(next === 'foreground' ? 'agents.graph.detail.movedForeground' : 'agents.graph.detail.movedBackground'), 'success');
      onRefresh();
    } catch (err) {
      showToast(err instanceof Error ? err.message : String(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  const endRun = async () => {
    disarm();
    setBusy(true);
    try {
      // Ending a live runtime needs the backend-specific identifiers the
      // running-agents snapshot holds (Claude → composite_key, Codex/OpenCode →
      // base_session_id, orphan → pid); session_id alone can't resolve the
      // teardown. Resolve the live row for this session at click time so the
      // graph node stays free of transient process identifiers.
      const snap = await api.getRunningAgents();
      const rows = (snap.ok ? snap.agents : []).filter((a) => a.session_id === node.session_id);
      // A session can have multiple backend rows; the liveness merge picked one
      // state for the node. End the row whose state made the node live (else the
      // idle/persisted row could be targeted instead of the active one), then
      // fall back to backend match, then any row for the session.
      const row =
        rows.find((a) => a.state === node.status && a.backend === node.agent_backend) ??
        rows.find((a) => a.state === node.status) ??
        rows.find((a) => a.backend === node.agent_backend) ??
        rows[0];
      if (!row) {
        showToast(t('agents.graph.detail.endGone'), 'warning');
        onRefresh();
        return;
      }
      const result = await api.endRunningAgent({
        backend: row.backend,
        state: row.state,
        session_id: row.session_id,
        composite_key: row.composite_key,
        base_session_id: row.base_session_id,
        pid: row.pid,
      });
      if (result.ok) {
        showToast(t('agents.running.endedToast'), 'success');
        onRefresh();
      } else {
        showToast(t('agents.running.endFailedToast', { error: result.error || 'failed' }), 'error');
      }
    } catch (err) {
      showToast(err instanceof Error ? err.message : String(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3.5">
      {/* Header: status + visibility pills + close */}
      <div className="flex items-center gap-2">
        <span
          className={clsx(
            'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold',
            statusPillClass(meta.tone),
          )}
        >
          <span className={clsx('size-1.5 rounded-full', meta.dotClass)} />
          {t(meta.labelKey)}
          {timeLabel !== '—' && <span className="font-mono text-[10px] opacity-80">· {timeLabel}</span>}
        </span>
        {node.visibility && (
          <span className="inline-flex items-center gap-1 rounded-full border border-border-strong bg-foreground/[0.04] px-2 py-0.5 text-[10px] text-muted">
            {background ? <EyeOff className="size-3" /> : <Eye className="size-3" />}
            {t(background ? 'agents.graph.detail.background' : 'agents.graph.detail.foreground')}
          </span>
        )}
        <span className="flex-1" />
        <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label={t('common.close')} className="size-6">
          <X className="size-3.5" />
        </Button>
      </div>

      {/* Title + agent line */}
      <div className="flex flex-col gap-1">
        <div className="text-[16px] font-bold text-foreground">{nodeDisplayTitle(node)}</div>
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
          {node.agent_name && <span className="font-medium text-foreground">{node.agent_name}</span>}
          {node.agent_backend && (
            <span className="rounded border border-border-strong bg-foreground/[0.04] px-1 font-mono text-[9px] font-bold uppercase">
              {node.agent_backend}
            </span>
          )}
          {node.model && <span className="font-mono">{node.model}</span>}
          {node.reasoning_effort && <span className="font-mono">· {node.reasoning_effort}</span>}
        </div>
      </div>

      <div className="h-px bg-border" />

      {/* Facts */}
      <div className="flex flex-col gap-2.5">
        <Fact label={t('agents.graph.detail.project')}>
          <span className="inline-flex items-center gap-1.5">
            <FolderClosed className="size-3 text-muted" />
            {node.scope_label ?? t('agents.graph.detail.standalone')}
          </span>
        </Fact>
        {node.workdir && (
          <Fact label={t('agents.graph.detail.workdir')}>
            <span className="break-all font-mono text-[11px]">{node.workdir}</span>
          </Fact>
        )}
        {node.visibility && (
          <Fact label={t('agents.graph.detail.visibility')}>
            <span className="inline-flex items-center gap-1.5">
              {background ? <EyeOff className="size-3" /> : <Eye className="size-3" />}
              {t(background ? 'agents.graph.detail.visibilityBackground' : 'agents.graph.detail.visibilityForeground')}
            </span>
          </Fact>
        )}
        <Fact label={t('agents.graph.detail.startedBy')}>
          {lineage.spawnedBy ? (
            <button
              type="button"
              onClick={() => onSelectNode(lineage.spawnedBy!)}
              className="inline-flex items-center gap-1.5 text-mint hover:underline"
            >
              <CornerLeftUp className="size-3" />
              {callerTitle(lineage.spawnedBy)}
            </button>
          ) : (
            <span className="inline-flex items-center gap-1.5 text-muted">
              <Bot className="size-3" />
              {triggerLabel}
            </span>
          )}
        </Fact>
        <Fact label={t('agents.graph.detail.reportsTo')}>
          {lineage.callbackTo ? (
            <span className="inline-flex items-center gap-1.5 text-cyan">
              <CornerDownRight className="size-3" />
              {callerTitle(lineage.callbackTo)}
              <span className="text-muted">· {t(`agents.graph.detail.callback.${lineage.callbackStatus ?? 'pending'}`)}</span>
            </span>
          ) : (
            <span className="text-muted">—</span>
          )}
        </Fact>
        <Fact label={t('agents.graph.detail.trigger')}>
          <span className="font-mono text-[11px] text-muted">{triggerLabel}</span>
        </Fact>
      </div>

      {/* Runs timeline */}
      {node.runs && node.runs.length > 0 && (
        <>
          <div className="flex items-center justify-between">
            <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">
              {t('agents.graph.detail.runsTitle')}
            </span>
            <Link to="/harness?tab=runs" className="text-[11px] font-medium text-cyan hover:underline">
              {t('agents.graph.detail.viewAllInHarness')}
            </Link>
          </div>
          <div className="flex flex-col gap-1">
            {node.runs.map((run) => (
              <Link
                key={run.id}
                to={`/harness?tab=runs&run=${encodeURIComponent(run.id)}`}
                className="flex items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[11px] transition hover:border-border-strong"
              >
                <span className={clsx('size-1.5 shrink-0 rounded-full', statusMeta(runStatus(run.status)).dotClass)} />
                <code className="font-mono text-foreground">{run.id}</code>
                <span className="text-muted">{t(statusMeta(runStatus(run.status)).labelKey)}</span>
                <span className="flex-1" />
                <span className="font-mono text-[10px] text-muted">{formatElapsed(runElapsedSeconds(run))}</span>
              </Link>
            ))}
          </div>
        </>
      )}

      {/* Actions */}
      <div className="flex flex-col gap-2 pt-1">
        {/* Open-chat only for openable sessions — an internal private-agent-run
            session has no chat to open (openable_in_chat=false). */}
        {node.openable_in_chat && (
          <Link
            to={`/chat/${encodeURIComponent(node.session_id)}`}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-lg border border-mint/40 bg-mint-soft text-[13px] font-semibold text-mint transition hover:brightness-110"
          >
            <MessageSquare className="size-3.5" />
            {t('agents.graph.detail.openChat')}
          </Link>
        )}
        <div className="flex items-center gap-2">
          {node.visibility && (
            <Button type="button" variant="outline" size="xs" onClick={toggleVisibility} disabled={busy} className="flex-1">
              {busy ? <Loader2 className="size-3 animate-spin" /> : background ? <Eye className="size-3" /> : <EyeOff className="size-3" />}
              {t(background ? 'agents.graph.detail.toForeground' : 'agents.graph.detail.toBackground')}
            </Button>
          )}
          {node.live && (() => {
            // Per-state end action (Stop / Disconnect / Kill), matching the old
            // running list: active + orphan are destructive (2-step confirm),
            // idle disconnect is non-destructive (single click).
            const meta = END_META[node.status as EndState] ?? END_META.active;
            const EndIcon = meta.Icon;
            return (
              <Button
                type="button"
                variant={armEnd ? 'destructive' : 'destructive-soft'}
                size="xs"
                onClick={() => (meta.needsConfirm && !armEnd ? arm() : endRun())}
                onBlur={() => disarm()}
                disabled={busy}
                className="flex-1"
              >
                {busy ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : armEnd ? null : (
                  <EndIcon className="size-3" />
                )}
                {armEnd ? t('agents.running.confirmEnd') : t(meta.labelKey)}
              </Button>
            );
          })()}
        </div>
      </div>
    </div>
  );
};

// Per-state end action, matching the old running list. Idle disconnect is
// non-destructive (no confirm); active Stop and orphan Kill require a 2nd click.
type EndState = 'active' | 'idle' | 'orphan';
const END_META: Record<EndState, { labelKey: string; Icon: typeof Square; needsConfirm: boolean }> = {
  active: { labelKey: 'agents.running.endActive', Icon: Square, needsConfirm: true },
  idle: { labelKey: 'agents.running.endIdle', Icon: Power, needsConfirm: false },
  orphan: { labelKey: 'agents.running.endOrphan', Icon: Trash2, needsConfirm: true },
};

// A run row's stored status is already normalized to the run vocabulary; map it
// onto the node status vocabulary the shared statusMeta understands (a live
// run's ``running`` reuses the ``active`` dot).
function runStatus(status: string): AgentGraphStatus {
  if (status === 'running') return 'active';
  const known: AgentGraphStatus[] = ['queued', 'succeeded', 'failed', 'canceled', 'idle', 'active', 'orphan'];
  return (known as string[]).includes(status) ? (status as AgentGraphStatus) : 'idle';
}

function statusPillClass(tone: string): string {
  if (tone === 'mint') return 'border-mint/40 bg-mint-soft text-mint';
  if (tone === 'gold') return 'border-gold/40 bg-gold/10 text-gold';
  if (tone === 'cyan') return 'border-cyan/40 bg-cyan-soft text-cyan';
  if (tone === 'destructive') return 'border-destructive/40 bg-destructive/10 text-destructive';
  return 'border-border-strong bg-foreground/[0.04] text-muted';
}

const Fact: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="flex items-start gap-3">
    <span className="w-16 shrink-0 pt-0.5 font-mono text-[10px] font-bold uppercase tracking-wide text-muted">{label}</span>
    <div className="min-w-0 flex-1 text-[12px] text-foreground">{children}</div>
  </div>
);
