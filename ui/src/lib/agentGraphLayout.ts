// Layered left→right layout for the run-graph canvas, backed by dagre.
//
// Why dagre (over elk): the graph is a call-tree forest — spawn + trigger edges
// form the hierarchy — which is exactly dagre's layered ("rank") model. It is
// synchronous (no async layout round-trip to reconcile against React state on
// every SSE refresh) and far lighter than the elkjs WASM/JS port, and it is the
// canonical React Flow layouting pairing. Only the desktop canvas imports this,
// so mobile never pulls dagre into its path.
//
// Only spawn + trigger edges are fed to the ranker: callback edges point back
// toward the caller and would fight the left→right layering if they influenced
// ranks. They are still drawn — just not used to place nodes.

import dagre from '@dagrejs/dagre';

// Node box sizes; shared with the node components so the layout reserves the
// right footprint. Session cards are wider; trigger chips are compact.
export const SESSION_NODE_WIDTH = 264;
export const SESSION_NODE_HEIGHT = 92;
export const TRIGGER_NODE_WIDTH = 208;
export const TRIGGER_NODE_HEIGHT = 68;

export type LayoutNode = { id: string; width: number; height: number };
export type LayoutEdge = { source: string; target: string };
export type LayoutPosition = { x: number; y: number };

export type LayoutOptions = {
  nodesep?: number;
  ranksep?: number;
  marginx?: number;
  marginy?: number;
};

// Returns top-left positions keyed by node id (dagre yields centers; React Flow
// wants top-left, so we offset by half the node box).
export function layoutLR(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  options: LayoutOptions = {},
): Map<string, LayoutPosition> {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({
    rankdir: 'LR',
    nodesep: options.nodesep ?? 26,
    ranksep: options.ranksep ?? 104,
    marginx: options.marginx ?? 28,
    marginy: options.marginy ?? 28,
  });
  graph.setDefaultEdgeLabel(() => ({}));

  for (const node of nodes) {
    graph.setNode(node.id, { width: node.width, height: node.height });
  }
  for (const edge of edges) {
    // Guard: a layout edge may reference a node that was filtered out.
    if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
      graph.setEdge(edge.source, edge.target);
    }
  }

  dagre.layout(graph);

  const positions = new Map<string, LayoutPosition>();
  for (const node of nodes) {
    const laid = graph.node(node.id);
    if (laid) {
      positions.set(node.id, { x: laid.x - node.width / 2, y: laid.y - node.height / 2 });
    }
  }
  return positions;
}
