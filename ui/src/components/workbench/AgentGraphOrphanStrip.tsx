import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Loader2, Trash2 } from 'lucide-react';
import clsx from 'clsx';

import type { RunningAgent } from '../../context/ApiContext';
import { Button } from '../ui/button';
import { BACKEND_LABEL, BACKEND_TEXT, type Backend } from '../../lib/backendAccent';
import { formatElapsed } from '../../lib/agentGraph';

interface AgentGraphOrphanStripProps {
  // Session-less live rows (orphan processes) from GET /api/running-agents.
  orphans: RunningAgent[];
  onKill: (orphan: RunningAgent) => Promise<void>;
}

// Contract A3: session-less orphan processes are NOT graph nodes (they have no
// session, lineage, or chat). The run graph replaced the flat running list, so
// this strip above the canvas preserves the ability to SEE and Kill leaked
// processes. Purely client-side over the existing running-agents payload; the
// graph payload is unchanged. Mobile renders the same strip above its list.
export const AgentGraphOrphanStrip: React.FC<AgentGraphOrphanStripProps> = ({ orphans, onKill }) => {
  const { t } = useTranslation();
  if (orphans.length === 0) return null;
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-amber/40 bg-amber/[0.05] px-4 py-3">
      <div className="flex items-center gap-2">
        <AlertTriangle className="size-3.5 shrink-0 text-amber-500" />
        <span className="text-[12px] font-semibold text-foreground">
          {t('agents.graph.orphans.title', { count: orphans.length })}
        </span>
      </div>
      <div className="text-[11px] text-muted">{t('agents.graph.orphans.description')}</div>
      <div className="flex flex-col gap-1.5">
        {orphans.map((orphan) => (
          <OrphanRow
            key={orphan.composite_key ?? `${orphan.backend}:${orphan.pid ?? orphan.native_session_id ?? ''}`}
            orphan={orphan}
            onKill={onKill}
          />
        ))}
      </div>
    </div>
  );
};

const OrphanRow: React.FC<{ orphan: RunningAgent; onKill: (o: RunningAgent) => Promise<void> }> = ({
  orphan,
  onKill,
}) => {
  const { t } = useTranslation();
  const [armed, setArmed] = useState(false);
  const [killing, setKilling] = useState(false);
  const mounted = useRef(true);
  const disarm = useRef<number | null>(null);
  useEffect(
    () => () => {
      mounted.current = false;
      if (disarm.current != null) window.clearTimeout(disarm.current);
    },
    [],
  );

  const backendLabel = BACKEND_LABEL[orphan.backend as Backend] ?? orphan.backend;
  const backendClass = BACKEND_TEXT[orphan.backend as Backend] ?? 'text-muted';

  // Two-step confirm: killing an orphan sends SIGTERM/SIGKILL to the leaked PID.
  const handleClick = () => {
    if (killing) return;
    if (!armed) {
      setArmed(true);
      if (disarm.current != null) window.clearTimeout(disarm.current);
      disarm.current = window.setTimeout(() => {
        if (mounted.current) setArmed(false);
      }, 3000);
      return;
    }
    setArmed(false);
    if (disarm.current != null) window.clearTimeout(disarm.current);
    setKilling(true);
    void onKill(orphan).finally(() => {
      if (mounted.current) setKilling(false);
    });
  };

  return (
    <div className="flex min-w-0 items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2 text-[11px]">
      <span className={clsx('font-mono text-[10px] font-bold uppercase tracking-wide', backendClass)}>
        {backendLabel}
      </span>
      {typeof orphan.pid === 'number' && (
        <span className="font-mono text-[10px] text-muted">
          pid {orphan.pid}
          {orphan.pid_shared && <span className="ml-1 text-amber-500">{t('agents.running.pidShared')}</span>}
        </span>
      )}
      {orphan.workdir && (
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted" title={orphan.workdir}>
          {orphan.workdir}
        </span>
      )}
      <span className="flex-1" />
      {orphan.elapsed_seconds != null && (
        <span className="shrink-0 font-mono text-[10px] text-muted">{formatElapsed(orphan.elapsed_seconds)}</span>
      )}
      <Button
        type="button"
        variant={armed ? 'destructive' : 'destructive-soft'}
        size="xs"
        onClick={handleClick}
        disabled={killing}
        className="h-7 shrink-0"
      >
        {killing ? (
          <Loader2 className="size-3 animate-spin" />
        ) : armed ? (
          t('agents.running.confirmEnd')
        ) : (
          <>
            <Trash2 className="size-3" />
            {t('agents.running.endOrphan')}
          </>
        )}
      </Button>
    </div>
  );
};
