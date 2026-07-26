import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { CalendarClock, CornerDownRight, Eye } from 'lucide-react';
import clsx from 'clsx';

import {
  type AgentGraphEdge,
  type AgentGraphNode,
  type AgentGraphTriggerNode,
  buildGraphForest,
} from '../../lib/agentGraph';
import { AgentGraphNodeCard } from './AgentGraphNodeCard';

interface AgentGraphMobileListProps {
  nodes: AgentGraphNode[];
  edges: AgentGraphEdge[];
  triggerNodes: AgentGraphTriggerNode[];
  selectedId: string | null;
  onSelectNode: (sessionId: string) => void;
  // Tapping a trigger pill opens the same right-side detail panel a node does
  // (A11) — no canvas on mobile, but the chip is still an equal citizen.
  onSelectTrigger: (definitionId: string) => void;
}

// Mobile fallback: the same graph payload rendered as a spawn-tree indented
// list (no canvas). Depth comes from the spawn forest; a triggered lineage root
// shows its Task/Watch source as a small chip above the card. Disabled triggers
// are already filtered out of `triggerNodes` upstream (A11), so no pill for them
// unless the shared "show disabled" preference is on.
export const AgentGraphMobileList: React.FC<AgentGraphMobileListProps> = ({
  nodes,
  edges,
  triggerNodes,
  selectedId,
  onSelectNode,
  onSelectTrigger,
}) => {
  const { t } = useTranslation();
  const rows = useMemo(() => buildGraphForest(nodes, edges, triggerNodes), [nodes, edges, triggerNodes]);

  return (
    <div className="flex flex-col gap-2">
      {rows.map(({ node, depth, trigger }) => (
        <div
          key={node.session_id}
          style={{ marginLeft: depth * 18 }}
          className="flex flex-col gap-1"
        >
          {depth > 0 && (
            <span className="flex items-center gap-1 text-[10px] text-muted">
              <CornerDownRight className="size-3" />
            </span>
          )}
          {trigger && (
            <button
              type="button"
              onClick={() => onSelectTrigger(trigger.definition_id)}
              title={trigger.name ?? trigger.definition_id}
              className={clsx(
                'inline-flex w-fit items-center gap-1 rounded-md border border-violet/40 bg-violet-soft px-1.5 py-0.5 text-[10px] font-medium text-violet transition hover:brightness-110',
                !trigger.enabled && 'opacity-60',
              )}
            >
              {trigger.definition_type === 'watch' ? <Eye className="size-2.5" /> : <CalendarClock className="size-2.5" />}
              {trigger.name ?? trigger.definition_id}
              {!trigger.enabled && <span className="text-muted"> · {t('agents.graph.trigger.disabled')}</span>}
            </button>
          )}
          <div className="h-[92px]">
            <AgentGraphNodeCard
              node={node}
              selected={node.session_id === selectedId}
              onClick={() => onSelectNode(node.session_id)}
            />
          </div>
        </div>
      ))}
      {rows.length === 0 && (
        <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-10 text-center text-[12px] text-muted">
          {t('agents.graph.empty')}
        </div>
      )}
    </div>
  );
};
