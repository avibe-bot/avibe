import { useTranslation } from 'react-i18next';
import { Check, EyeOff, FolderClosed, X } from 'lucide-react';
import clsx from 'clsx';

import { BACKEND_TEXT, type Backend } from '../../lib/backendAccent';
import { formatRelativeTime } from '../../lib/relativeTime';
import {
  type AgentGraphNode,
  formatElapsed,
  isBackground,
  nodeDisplayTitle,
  statusMeta,
} from '../../lib/agentGraph';

// Border/tint by status tone. Selected wins (mint ring + glow, per spec frame
// anu5U 选中态). Failed nodes carry a destructive tint; live-active a mint
// hint; everything else the neutral surface. Non-live nodes are dimmed by the
// caller via ``faded``.
function shellClass(node: AgentGraphNode, selected: boolean): string {
  if (selected) return 'border-mint/70 bg-mint-soft shadow-[0_0_20px_-6px_rgba(91,255,160,0.55)]';
  const tone = statusMeta(node.status).tone;
  if (tone === 'destructive') return 'border-destructive/40 bg-destructive/[0.05]';
  if (node.status === 'active') return 'border-mint/40 bg-surface';
  if (node.status === 'queued') return 'border-gold/40 bg-surface';
  return 'border-border bg-surface';
}

interface AgentGraphNodeCardProps {
  node: AgentGraphNode;
  selected?: boolean;
  // Dim when a hovered node's up/down-stream highlight excludes this one.
  faded?: boolean;
  onClick?: () => void;
  onDoubleClick?: () => void;
  className?: string;
}

// One session card — the shared visual used both as a React Flow canvas node
// and as a mobile grouped-list row. Fills its parent (the parent owns the box
// size so the dagre layout and the card stay in lockstep).
export const AgentGraphNodeCard: React.FC<AgentGraphNodeCardProps> = ({
  node,
  selected = false,
  faded = false,
  onClick,
  onDoubleClick,
  className,
}) => {
  const { t } = useTranslation();
  const backendClass = BACKEND_TEXT[node.agent_backend as Backend] ?? 'text-muted';
  const background = isBackground(node);
  const timeLabel = node.live
    ? formatElapsed(node.elapsed_seconds)
    : formatRelativeTime(node.last_active_at ?? node.created_at, t);

  return (
    <button
      type="button"
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      title={nodeDisplayTitle(node)}
      className={clsx(
        'flex h-full w-full flex-col gap-1.5 rounded-xl border px-3 py-2.5 text-left transition',
        shellClass(node, selected),
        !node.live && !selected && 'opacity-65',
        faded && 'opacity-25',
        onClick && 'hover:border-border-strong',
        className,
      )}
    >
      {/* Header: status glyph · agent · backend badge · visibility eye */}
      <div className="flex min-w-0 items-center gap-1.5">
        <StatusGlyph node={node} />
        <span className="truncate text-[12px] font-semibold text-foreground">
          {node.agent_name ?? '—'}
        </span>
        {node.agent_backend && (
          <span
            className={clsx(
              'shrink-0 rounded border border-border-strong bg-foreground/[0.04] px-1 py-0 font-mono text-[9px] font-bold',
              backendClass,
            )}
          >
            {node.agent_backend}
          </span>
        )}
        <span className="flex-1" />
        {background && <EyeOff className="size-3 shrink-0 text-muted" aria-label={t('agents.graph.detail.background')} />}
      </div>

      {/* Title */}
      <div className="min-w-0 truncate text-[13px] font-semibold text-foreground">
        {nodeDisplayTitle(node)}
      </div>

      {/* Footer: scope (or 独立) · elapsed / relative time */}
      <div className="flex min-w-0 items-center gap-1.5 text-[11px] text-muted">
        <FolderClosed className="size-3 shrink-0" />
        <span className="min-w-0 flex-1 truncate">
          {node.scope_label ?? t('agents.graph.detail.standalone')}
        </span>
        <span className="shrink-0 font-mono text-[10px]">{timeLabel}</span>
      </div>
    </button>
  );
};

// Status glyph: colored dot (live/queued), check (succeeded), or cross
// (failed/canceled) — mirrors the spec frame node states.
const StatusGlyph: React.FC<{ node: AgentGraphNode }> = ({ node }) => {
  const meta = statusMeta(node.status);
  if (meta.glyph === 'check') return <Check className="size-3 shrink-0 text-muted" />;
  if (meta.glyph === 'cross') {
    return <X className={clsx('size-3 shrink-0', meta.tone === 'destructive' ? 'text-destructive' : 'text-muted')} />;
  }
  return <span className={clsx('size-2 shrink-0 rounded-full', meta.dotClass, node.status === 'active' && 'animate-pulse')} />;
};
