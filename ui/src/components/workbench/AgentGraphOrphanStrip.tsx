import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Loader2, Power, Square, Trash2 } from 'lucide-react';
import clsx from 'clsx';

import type { RunningAgent } from '../../context/ApiContext';
import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { BACKEND_LABEL, BACKEND_TEXT, type Backend } from '../../lib/backendAccent';
import { formatElapsed } from '../../lib/agentGraph';

// Contract A3 (+ review r6): the run graph is session-centric, so a live
// running-agents row with no session_id has no node. This strip above the
// canvas preserves the old flat list's ability to SEE and end EVERY such
// session-less runtime — not just orphans — with its actual state and a
// state-appropriate action. Purely client-side over the running-agents
// payload; the graph payload is unchanged.

// Per-state presentation: dot/label + end action (Stop / Disconnect / Kill).
// Idle disconnect is non-destructive (single click); active/orphan confirm.
type LiveState = 'active' | 'idle' | 'orphan';
const STATE_META: Record<
  LiveState,
  { dotClass: string; stateKey: string; endKey: string; Icon: typeof Square; needsConfirm: boolean }
> = {
  active: { dotClass: 'bg-mint', stateKey: 'agents.running.stateActive', endKey: 'agents.running.endActive', Icon: Square, needsConfirm: true },
  idle: { dotClass: 'bg-muted', stateKey: 'agents.running.stateIdle', endKey: 'agents.running.endIdle', Icon: Power, needsConfirm: false },
  orphan: { dotClass: 'bg-amber-500', stateKey: 'agents.running.stateOrphan', endKey: 'agents.running.endOrphan', Icon: Trash2, needsConfirm: true },
};
const stateMeta = (state: string) => STATE_META[(state as LiveState)] ?? STATE_META.orphan;

interface AgentGraphOrphanStripProps {
  // Session-less live rows (any state) from GET /api/running-agents.
  rows: RunningAgent[];
  onEnd: (row: RunningAgent) => Promise<void>;
}

export const AgentGraphOrphanStrip: React.FC<AgentGraphOrphanStripProps> = ({ rows, onEnd }) => {
  const { t } = useTranslation();
  if (rows.length === 0) return null;
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-amber-500/40 bg-amber-500/[0.05] px-4 py-3">
      <div className="flex items-center gap-2">
        <AlertTriangle className="size-3.5 shrink-0 text-amber-500" />
        <span className="text-[12px] font-semibold text-foreground">
          {t('agents.graph.orphans.title', { count: rows.length })}
        </span>
      </div>
      <div className="text-[11px] text-muted">{t('agents.graph.orphans.description')}</div>
      <div className="flex flex-col gap-1.5">
        {rows.map((row) => (
          <OrphanRow
            // composite_key is the unique identifier when present; otherwise
            // combine every stable identifier the row carries so two session-less
            // rows from the same backend (e.g. OpenCode active rows with only a
            // base_session_id, no pid/native id) can't collide onto one React key
            // and swap OrphanRow state (a stuck/misdirected confirm or spinner).
            key={
              row.composite_key ??
              [row.backend, row.base_session_id, row.native_session_id, row.pid, row.workdir]
                .map((v) => v ?? '')
                .join('|')
            }
            row={row}
            onEnd={onEnd}
          />
        ))}
      </div>
    </div>
  );
};

const OrphanRow: React.FC<{ row: RunningAgent; onEnd: (r: RunningAgent) => Promise<void> }> = ({
  row,
  onEnd,
}) => {
  const { t } = useTranslation();
  const [armed, setArmed] = useState(false);
  const [ending, setEnding] = useState(false);
  const mounted = useRef(true);
  const disarm = useRef<number | null>(null);
  useEffect(
    () => () => {
      mounted.current = false;
      if (disarm.current != null) window.clearTimeout(disarm.current);
    },
    [],
  );

  const meta = stateMeta(row.state);
  const EndIcon = meta.Icon;
  const backendLabel = BACKEND_LABEL[row.backend as Backend] ?? row.backend;
  const backendClass = BACKEND_TEXT[row.backend as Backend] ?? 'text-muted';

  const handleClick = () => {
    if (ending) return;
    if (meta.needsConfirm && !armed) {
      setArmed(true);
      if (disarm.current != null) window.clearTimeout(disarm.current);
      disarm.current = window.setTimeout(() => {
        if (mounted.current) setArmed(false);
      }, 3000);
      return;
    }
    setArmed(false);
    if (disarm.current != null) window.clearTimeout(disarm.current);
    setEnding(true);
    void onEnd(row).finally(() => {
      if (mounted.current) setEnding(false);
    });
  };

  return (
    <div className="flex min-w-0 items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2 text-[11px]">
      <Badge variant="secondary" className="font-mono text-[9px] uppercase">
        <span className={clsx('size-1.5 rounded-full', meta.dotClass)} />
        {t(meta.stateKey)}
      </Badge>
      <span className={clsx('font-mono text-[10px] font-bold uppercase tracking-wide', backendClass)}>
        {backendLabel}
      </span>
      {typeof row.pid === 'number' && (
        <span className="font-mono text-[10px] text-muted">
          pid {row.pid}
          {row.pid_shared && <span className="ml-1 text-amber-500">{t('agents.running.pidShared')}</span>}
        </span>
      )}
      {row.workdir && (
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted" title={row.workdir}>
          {row.workdir}
        </span>
      )}
      <span className="flex-1" />
      {row.elapsed_seconds != null && (
        <span className="shrink-0 font-mono text-[10px] text-muted">{formatElapsed(row.elapsed_seconds)}</span>
      )}
      <Button
        type="button"
        variant={armed ? 'destructive' : 'destructive-soft'}
        size="xs"
        onClick={handleClick}
        disabled={ending}
        className="h-7 shrink-0"
      >
        {ending ? (
          <Loader2 className="size-3 animate-spin" />
        ) : armed ? (
          t('agents.running.confirmEnd')
        ) : (
          <>
            <EndIcon className="size-3" />
            {t(meta.endKey)}
          </>
        )}
      </Button>
    </div>
  );
};
