import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../context/ThemeContext';
import {
  type AgentGraphEdge,
  type AgentGraphNode,
  type AgentGraphTriggerNode,
  triggerRefId,
} from '../../lib/agentGraph';
import {
  SESSION_NODE_HEIGHT,
  SESSION_NODE_WIDTH,
  TRIGGER_NODE_HEIGHT,
  TRIGGER_NODE_WIDTH,
  layoutLR,
} from '../../lib/agentGraphLayout';
import { AgentGraphNodeCard } from './AgentGraphNodeCard';
import { AgentGraphTriggerChip } from './AgentGraphTriggerChip';

// ── custom node data + components ────────────────────────────────────────────

type SessionNodeData = {
  node: AgentGraphNode;
  selected: boolean;
  faded: boolean;
  onSelect: (id: string) => void;
  onOpenChat: (id: string) => void;
};
type TriggerNodeData = {
  trigger: AgentGraphTriggerNode;
  faded: boolean;
  onSelect: (definitionId: string) => void;
};

// Hidden handles anchor the edges; the card/chip fills the sized RF node box.
const HANDLE_CLASS = '!h-1 !w-1 !min-w-0 !border-0 !bg-transparent';

const SessionRFNode: React.FC<NodeProps> = ({ data }) => {
  const d = data as unknown as SessionNodeData;
  return (
    <>
      <Handle type="target" position={Position.Left} className={HANDLE_CLASS} isConnectable={false} />
      <AgentGraphNodeCard
        node={d.node}
        selected={d.selected}
        faded={d.faded}
        onClick={() => d.onSelect(d.node.session_id)}
        // Double-click opens the chat only for openable sessions (internal
        // private-agent-run nodes have none).
        onDoubleClick={() => d.node.openable_in_chat && d.onOpenChat(d.node.session_id)}
      />
      <Handle type="source" position={Position.Right} className={HANDLE_CLASS} isConnectable={false} />
    </>
  );
};

const TriggerRFNode: React.FC<NodeProps> = ({ data }) => {
  const d = data as unknown as TriggerNodeData;
  return (
    <>
      <AgentGraphTriggerChip trigger={d.trigger} faded={d.faded} onClick={() => d.onSelect(d.trigger.definition_id)} />
      <Handle type="source" position={Position.Right} className={HANDLE_CLASS} isConnectable={false} />
    </>
  );
};

// Module-const so React Flow doesn't warn about a new nodeTypes object each render.
const NODE_TYPES = { session: SessionRFNode, trigger: TriggerRFNode };

// ── edge styling ─────────────────────────────────────────────────────────────

function edgeVars(kind: AgentGraphEdge['kind']): { color: string; dashed: boolean } {
  if (kind === 'spawn') return { color: 'var(--mint)', dashed: false };
  if (kind === 'callback') return { color: 'var(--cyan)', dashed: true };
  return { color: 'var(--violet)', dashed: true }; // trigger
}

// Build an undirected adjacency map so hovering a node can highlight its whole
// up/down-stream chain (spec: "hover 节点高亮上下游链路").
function buildAdjacency(edges: AgentGraphEdge[]): Map<string, Set<string>> {
  const adj = new Map<string, Set<string>>();
  const link = (a: string, b: string) => {
    if (!adj.has(a)) adj.set(a, new Set());
    adj.get(a)!.add(b);
  };
  for (const e of edges) {
    link(e.from, e.to);
    link(e.to, e.from);
  }
  return adj;
}

function reachable(start: string, adj: Map<string, Set<string>>): Set<string> {
  const seen = new Set<string>([start]);
  const stack = [start];
  while (stack.length) {
    const cur = stack.pop()!;
    for (const next of adj.get(cur) ?? []) {
      if (!seen.has(next)) {
        seen.add(next);
        stack.push(next);
      }
    }
  }
  return seen;
}

// ── props ────────────────────────────────────────────────────────────────────

interface AgentGraphCanvasProps {
  nodes: AgentGraphNode[];
  triggerNodes: AgentGraphTriggerNode[];
  edges: AgentGraphEdge[];
  selectedId: string | null;
  // Changes when the user changes a filter; a new value re-fits the viewport to
  // the new layout. Unchanged across SSE/poll refreshes so they keep the view.
  fitKey: string;
  onSelectNode: (sessionId: string) => void;
  onSelectTrigger: (definitionId: string) => void;
  onOpenChat: (sessionId: string) => void;
}

export const AgentGraphCanvas: React.FC<AgentGraphCanvasProps> = (props) => (
  // Provider lets the inner flow call fitView once nodes are measured.
  <ReactFlowProvider>
    <Flow {...props} />
  </ReactFlowProvider>
);

const Flow: React.FC<AgentGraphCanvasProps> = ({
  nodes,
  triggerNodes,
  edges,
  selectedId,
  fitKey,
  onSelectNode,
  onSelectTrigger,
  onOpenChat,
}) => {
  const { t } = useTranslation();
  const { resolvedTheme } = useTheme();
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const reactFlow = useReactFlow();
  const fittedRef = useRef(false);

  const adjacency = useMemo(() => buildAdjacency(edges), [edges]);
  const highlighted = useMemo(
    () => (hoveredId ? reachable(hoveredId, adjacency) : null),
    [hoveredId, adjacency],
  );

  // Layout only ranks along the call tree (spawn + trigger). Callback edges are
  // drawn but excluded from ranking so they don't fight the left→right layers.
  const layout = useMemo(() => {
    const layoutNodes = [
      ...nodes.map((n) => ({ id: n.session_id, width: SESSION_NODE_WIDTH, height: SESSION_NODE_HEIGHT })),
      ...triggerNodes.map((tr) => ({
        id: triggerRefId(tr.definition_id),
        width: TRIGGER_NODE_WIDTH,
        height: TRIGGER_NODE_HEIGHT,
      })),
    ];
    const layoutEdges = edges
      .filter((e) => e.kind === 'spawn' || e.kind === 'trigger')
      .map((e) => ({ source: e.from, target: e.to }));
    return layoutLR(layoutNodes, layoutEdges);
  }, [nodes, triggerNodes, edges]);

  // Compose the React Flow node list (sessions + trigger chips).
  const computedNodes = useMemo<Node[]>(() => {
    const out: Node[] = [];
    for (const node of nodes) {
      const pos = layout.get(node.session_id) ?? { x: 0, y: 0 };
      out.push({
        id: node.session_id,
        type: 'session',
        position: pos,
        draggable: false,
        style: { width: SESSION_NODE_WIDTH, height: SESSION_NODE_HEIGHT },
        data: {
          node,
          selected: node.session_id === selectedId,
          faded: !!highlighted && !highlighted.has(node.session_id),
          onSelect: onSelectNode,
          onOpenChat,
        } as unknown as Record<string, unknown>,
      });
    }
    for (const trigger of triggerNodes) {
      const id = triggerRefId(trigger.definition_id);
      const pos = layout.get(id) ?? { x: 0, y: 0 };
      out.push({
        id,
        type: 'trigger',
        position: pos,
        draggable: false,
        style: { width: TRIGGER_NODE_WIDTH, height: TRIGGER_NODE_HEIGHT },
        data: {
          trigger,
          faded: !!highlighted && !highlighted.has(id),
          onSelect: onSelectTrigger,
        } as unknown as Record<string, unknown>,
      });
    }
    return out;
  }, [nodes, triggerNodes, layout, selectedId, highlighted, onSelectNode, onSelectTrigger, onOpenChat]);

  const computedEdges = useMemo<Edge[]>(() => {
    return edges.map((edge) => {
      const { color, dashed } = edgeVars(edge.kind);
      const dim = !!highlighted && !(highlighted.has(edge.from) && highlighted.has(edge.to));
      return {
        id: `${edge.kind}:${edge.from}->${edge.to}`,
        source: edge.from,
        target: edge.to,
        type: 'default',
        style: {
          stroke: color,
          strokeWidth: 1.6,
          strokeDasharray: dashed ? '5 4' : undefined,
          opacity: dim ? 0.12 : 0.9,
        },
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 13, height: 13 },
      } satisfies Edge;
    });
  }, [edges, highlighted]);

  useEffect(() => setRfNodes(computedNodes), [computedNodes, setRfNodes]);
  useEffect(() => setRfEdges(computedEdges), [computedEdges, setRfEdges]);

  // A deliberate filter/layout change re-arms the fit guard so the next measured
  // layout re-fits; SSE/poll refreshes keep the same fitKey and preserve the view.
  useEffect(() => {
    fittedRef.current = false;
  }, [fitKey]);

  // Fit after the first non-empty layout is measured (and again after a fitKey
  // change re-arms the guard); plain SSE refreshes keep the viewport stable.
  useEffect(() => {
    if (!fittedRef.current && rfNodes.length) {
      fittedRef.current = true;
      requestAnimationFrame(() => reactFlow.fitView({ padding: 0.22, maxZoom: 1 }));
    }
  }, [rfNodes, reactFlow]);

  return (
    <div className="h-[600px] max-h-[72vh] w-full overflow-hidden rounded-2xl border border-border-strong bg-surface-3/60">
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={NODE_TYPES}
        colorMode={resolvedTheme}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        // Read-only graph: keep click selection but disable Delete/Backspace, or
        // the change handlers would drop a selected node/edge until next refresh.
        deleteKeyCode={null}
        proOptions={{ hideAttribution: true }}
        minZoom={0.2}
        maxZoom={1.75}
        onNodeMouseEnter={(_e, node) => setHoveredId(node.id)}
        onNodeMouseLeave={() => setHoveredId(null)}
        onPaneClick={() => setHoveredId(null)}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} className="opacity-40" />
        <Controls showInteractive={false} position="bottom-right" />
        <Panel position="bottom-left">
          <Legend t={t} />
        </Panel>
      </ReactFlow>
    </div>
  );
};

const Legend: React.FC<{ t: (k: string) => string }> = ({ t }) => (
  <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-xl border border-border-strong bg-surface/90 px-3 py-2 text-[11px] text-muted backdrop-blur">
    <LegendItem label={t('agents.graph.legend.spawn')}>
      <span className="h-[2px] w-5" style={{ background: 'var(--mint)' }} />
    </LegendItem>
    <LegendItem label={t('agents.graph.legend.callback')}>
      <span className="h-0 w-5 border-t-2 border-dashed" style={{ borderColor: 'var(--cyan)' }} />
    </LegendItem>
    <LegendItem label={t('agents.graph.legend.trigger')}>
      <span className="h-0 w-5 border-t-2 border-dashed" style={{ borderColor: 'var(--violet)' }} />
    </LegendItem>
    <LegendItem label={t('agents.graph.legend.background')}>
      <span className="text-muted">◎</span>
    </LegendItem>
  </div>
);

const LegendItem: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <span className="inline-flex items-center gap-1.5">
    <span className="inline-flex items-center">{children}</span>
    {label}
  </span>
);
