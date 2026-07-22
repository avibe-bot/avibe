// Shared types + pure helpers for the Agents · 运行图 (run graph).
//
// The wire shape is the frozen contract in
// `docs/plans/agents-run-graph-contract.md` §3 (GET /api/agents-graph). The
// only addition over that doc is the per-node `runs` array: the detail panel's
// "RUNS · 本会话" timeline needs the recent run rows, and the graph service is
// the one place that already has agent_runs open — embedding them avoids an
// N+1 fetch from the client. It is additive (no frozen field changes) and only
// the graph frontend consumes it.
//
// Everything here is transport/React-agnostic so the layout + forest builders
// stay unit-testable without a canvas.

// ── Wire types (frozen contract §3) ────────────────────────────────────────

// Live nodes carry a running-agents state; non-live nodes carry the latest run
// outcome. `live` on the node distinguishes the two families.
export type AgentGraphLiveStatus = 'active' | 'idle' | 'orphan';
export type AgentGraphTerminalStatus = 'queued' | 'succeeded' | 'failed' | 'canceled';
export type AgentGraphStatus = AgentGraphLiveStatus | AgentGraphTerminalStatus;

export type AgentGraphVisibility = 'foreground' | 'background';

// One recent run for a session (contract amendment A1; drives the detail-panel
// timeline). Elapsed is derived client-side from started/completed.
export type AgentGraphRunRow = {
  id: string;
  status: string;
  run_type: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type AgentGraphNode = {
  session_id: string;
  title: string | null;
  agent_name: string | null;
  agent_backend: string | null;
  model: string | null;
  reasoning_effort: string | null;
  status: AgentGraphStatus;
  live: boolean;
  // Absent until the M1 visibility column ships; absent ⇒ treat as foreground
  // AND hide the 移到前台/隐藏 actions (the PATCH would 400 otherwise).
  visibility?: AgentGraphVisibility;
  scope_id: string | null;
  project_id: string | null;
  scope_label: string | null;
  platform: string | null;
  workdir: string | null;
  openable_in_chat: boolean;
  created_at: string;
  last_active_at: string | null;
  elapsed_seconds: number | null;
  run_counts: { total: number; running: number };
  runs?: AgentGraphRunRow[];
};

export type AgentGraphTriggerNode = {
  definition_id: string;
  // 'scheduled' | 'watch' in practice; kept open for forward definitions.
  definition_type: string;
  name: string | null;
  schedule_label: string | null;
  enabled: boolean;
};

export type AgentGraphEdgeKind = 'spawn' | 'callback' | 'trigger';
export type AgentGraphCallbackStatus = 'pending' | 'sent' | 'failed' | 'skipped';

export type AgentGraphEdge = {
  kind: AgentGraphEdgeKind;
  // session_id, or `def:<definition_id>` for a trigger source.
  from: string;
  to: string;
  run_count?: number;
  last_run_id?: string | null;
  last_at?: string | null;
  // callback edges only.
  status?: AgentGraphCallbackStatus | null;
};

export type AgentGraphCounts = {
  active: number;
  idle: number;
  queued: number;
  ended: number;
  background: number;
  foreground: number;
  [key: string]: number;
};

export type AgentGraphResult = {
  ok: boolean;
  generated_at: string;
  window: string;
  counts: AgentGraphCounts;
  nodes: AgentGraphNode[];
  trigger_nodes: AgentGraphTriggerNode[];
  edges: AgentGraphEdge[];
  truncated: boolean;
};

// ── Query params ────────────────────────────────────────────────────────────

export type GraphWindow = '1h' | '6h' | '24h' | '7d';
export const GRAPH_WINDOWS: GraphWindow[] = ['1h', '6h', '24h', '7d'];

// `all` = every project, `standalone` = the NULL-scope (独立) bucket, else a
// concrete project_id. Mirrors the contract's `project` query param.
export type GraphProjectFilter = string; // 'all' | 'standalone' | <project_id>

export type AgentGraphParams = {
  window?: GraphWindow;
  project?: GraphProjectFilter;
  includeEnded?: boolean;
  includeBackground?: boolean;
};

// ── Presentation helpers ─────────────────────────────────────────────────────

// The trigger def id encoded on a trigger edge's `from`.
export const TRIGGER_PREFIX = 'def:';
export function triggerRefId(definitionId: string): string {
  return `${TRIGGER_PREFIX}${definitionId}`;
}
export function isTriggerRef(ref: string): boolean {
  return ref.startsWith(TRIGGER_PREFIX);
}
export function definitionIdFromRef(ref: string): string {
  return ref.startsWith(TRIGGER_PREFIX) ? ref.slice(TRIGGER_PREFIX.length) : ref;
}

// Per-status presentation, in one place (matches the spec frame anu5U). The
// dot/border tone maps to design tokens; `dim` fades ended sessions.
export type StatusTone = 'mint' | 'gold' | 'cyan' | 'muted' | 'destructive';
export type StatusMeta = {
  tone: StatusTone;
  dotClass: string;
  labelKey: string;
  dim: boolean;
  // Which glyph the node header shows: a colored dot, a check, or a cross.
  glyph: 'dot' | 'check' | 'cross';
};

const STATUS_META: Record<AgentGraphStatus, StatusMeta> = {
  active: { tone: 'mint', dotClass: 'bg-mint', labelKey: 'agents.graph.status.active', dim: false, glyph: 'dot' },
  queued: { tone: 'gold', dotClass: 'bg-gold', labelKey: 'agents.graph.status.queued', dim: false, glyph: 'dot' },
  idle: { tone: 'cyan', dotClass: 'bg-cyan', labelKey: 'agents.graph.status.idle', dim: false, glyph: 'dot' },
  orphan: { tone: 'gold', dotClass: 'bg-amber-500', labelKey: 'agents.graph.status.orphan', dim: false, glyph: 'dot' },
  succeeded: { tone: 'muted', dotClass: 'bg-muted', labelKey: 'agents.graph.status.succeeded', dim: true, glyph: 'check' },
  failed: { tone: 'destructive', dotClass: 'bg-destructive', labelKey: 'agents.graph.status.failed', dim: true, glyph: 'cross' },
  canceled: { tone: 'muted', dotClass: 'bg-muted', labelKey: 'agents.graph.status.canceled', dim: true, glyph: 'cross' },
};

export function statusMeta(status: AgentGraphStatus): StatusMeta {
  return STATUS_META[status] ?? STATUS_META.idle;
}

// A background session is dimmed/eye-off; absent visibility ⇒ foreground.
export function isBackground(node: Pick<AgentGraphNode, 'visibility'>): boolean {
  return node.visibility === 'background';
}

// Human-friendly duration: 12 → "12s", 185 → "3m", 3700 → "1h". Mirrors the
// running-list formatter so the graph and list read consistently.
export function formatElapsed(seconds: number | null | undefined): string {
  if (seconds == null) return '—';
  const s = Math.max(0, seconds);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

// Elapsed seconds for a run row: completed − started, or now − started while
// still open. Derived client-side since A1 carries timestamps, not a duration.
export function runElapsedSeconds(
  run: Pick<AgentGraphRunRow, 'started_at' | 'completed_at'>,
): number | null {
  if (!run.started_at) return null;
  const start = Date.parse(run.started_at);
  if (Number.isNaN(start)) return null;
  const end = run.completed_at ? Date.parse(run.completed_at) : Date.now();
  return Math.max(0, (end - start) / 1000);
}

// Fallback label when a node has no title: agent name + short session suffix.
export function nodeDisplayTitle(node: Pick<AgentGraphNode, 'title' | 'agent_name' | 'session_id'>): string {
  if (node.title && node.title.trim()) return node.title;
  const suffix = node.session_id.length > 6 ? node.session_id.slice(-6) : node.session_id;
  return node.agent_name ? `${node.agent_name} · ${suffix}` : suffix;
}

// ── Lineage derivation (facts panel) ─────────────────────────────────────────

export type NodeLineage = {
  // 启动方: caller session that spawned this one (latest spawn edge in).
  spawnedBy: string | null;
  // 汇报到: callback target + status (callback edge out).
  callbackTo: string | null;
  callbackStatus: AgentGraphCallbackStatus | null;
  // 触发: originating task/watch definition (trigger edge in).
  trigger: AgentGraphTriggerNode | null;
};

// Pick the most recent of two ISO timestamps (null sorts oldest).
function laterAt(a: string | null | undefined, b: string | null | undefined): number {
  return (a ? Date.parse(a) : 0) - (b ? Date.parse(b) : 0);
}

export function deriveLineage(
  sessionId: string,
  edges: AgentGraphEdge[],
  triggersById: Map<string, AgentGraphTriggerNode>,
): NodeLineage {
  let spawnedBy: AgentGraphEdge | null = null;
  let callback: AgentGraphEdge | null = null;
  let trigger: AgentGraphEdge | null = null;
  for (const edge of edges) {
    if (edge.kind === 'spawn' && edge.to === sessionId) {
      if (!spawnedBy || laterAt(edge.last_at, spawnedBy.last_at) > 0) spawnedBy = edge;
    } else if (edge.kind === 'callback' && edge.from === sessionId) {
      if (!callback || laterAt(edge.last_at, callback.last_at) > 0) callback = edge;
    } else if (edge.kind === 'trigger' && edge.to === sessionId) {
      if (!trigger || laterAt(edge.last_at, trigger.last_at) > 0) trigger = edge;
    }
  }
  return {
    spawnedBy: spawnedBy ? spawnedBy.from : null,
    callbackTo: callback ? callback.to : null,
    callbackStatus: callback ? (callback.status ?? 'pending') : null,
    trigger: trigger ? triggersById.get(definitionIdFromRef(trigger.from)) ?? null : null,
  };
}

// ── Forest builder (mobile grouped list) ─────────────────────────────────────

// One row of the mobile tree list: a node plus its indentation depth and the
// trigger (if any) that spawned its lineage root.
export type ForestRow = {
  node: AgentGraphNode;
  depth: number;
  trigger: AgentGraphTriggerNode | null;
};

// Ranking: live sessions first, then most-recently-active. Deterministic tie
// break on session_id so the list never reshuffles between refreshes.
function nodeOrder(a: AgentGraphNode, b: AgentGraphNode): number {
  if (a.live !== b.live) return a.live ? -1 : 1;
  const at = laterAt(b.last_active_at ?? b.created_at, a.last_active_at ?? a.created_at);
  if (at !== 0) return at;
  return a.session_id.localeCompare(b.session_id);
}

// Flatten the spawn forest into indented rows. A node's parent is the latest
// spawn caller that is also present in the node set; nodes with no such parent
// are roots. Cycles and diamonds are guarded by a visited set (each node is
// emitted once, under its latest parent). Triggered roots carry their trigger
// chip so the mobile row can show the Task/Watch source.
export function buildGraphForest(
  nodes: AgentGraphNode[],
  edges: AgentGraphEdge[],
  triggerNodes: AgentGraphTriggerNode[] = [],
): ForestRow[] {
  const byId = new Map(nodes.map((n) => [n.session_id, n]));
  const triggersById = new Map(triggerNodes.map((tr) => [tr.definition_id, tr]));

  // Resolve each node's spawn parent (latest incoming spawn edge whose source
  // is a real node) and its trigger source.
  const parentOf = new Map<string, string>();
  const parentEdgeAt = new Map<string, string | null | undefined>();
  const triggerOf = new Map<string, AgentGraphTriggerNode>();
  const triggerAtOf = new Map<string, string | null | undefined>();
  const childrenOf = new Map<string, string[]>();

  for (const edge of edges) {
    if (edge.kind === 'spawn' && byId.has(edge.from) && byId.has(edge.to) && edge.from !== edge.to) {
      const prev = parentOf.get(edge.to);
      if (!prev || laterAt(edge.last_at, parentEdgeAt.get(edge.to)) > 0) {
        parentOf.set(edge.to, edge.from);
        parentEdgeAt.set(edge.to, edge.last_at);
      }
    } else if (edge.kind === 'trigger' && byId.has(edge.to)) {
      const tr = triggersById.get(definitionIdFromRef(edge.from));
      // A session reused by multiple tasks/watches gets one trigger edge per
      // definition; keep the latest by last_at so the mobile chip matches the
      // detail panel's lineage (which also picks the newest trigger).
      if (tr && (!triggerOf.has(edge.to) || laterAt(edge.last_at, triggerAtOf.get(edge.to)) > 0)) {
        triggerOf.set(edge.to, tr);
        triggerAtOf.set(edge.to, edge.last_at);
      }
    }
  }
  for (const [child, parent] of parentOf) {
    if (!childrenOf.has(parent)) childrenOf.set(parent, []);
    childrenOf.get(parent)!.push(child);
  }

  const roots = nodes.filter((n) => !parentOf.has(n.session_id)).sort(nodeOrder);
  const rows: ForestRow[] = [];
  const visited = new Set<string>();

  const walk = (node: AgentGraphNode, depth: number, rootTrigger: AgentGraphTriggerNode | null) => {
    if (visited.has(node.session_id)) return;
    visited.add(node.session_id);
    const trigger = triggerOf.get(node.session_id) ?? (depth === 0 ? rootTrigger : null);
    rows.push({ node, depth, trigger });
    const kids = (childrenOf.get(node.session_id) ?? [])
      .map((id) => byId.get(id))
      .filter((n): n is AgentGraphNode => !!n)
      .sort(nodeOrder);
    for (const kid of kids) walk(kid, depth + 1, null);
  };

  for (const root of roots) walk(root, 0, triggerOf.get(root.session_id) ?? null);
  // Any node left unvisited (a cycle with no external root) is surfaced flat so
  // it never silently disappears from the list.
  for (const node of nodes) if (!visited.has(node.session_id)) rows.push({ node, depth: 0, trigger: null });
  return rows;
}
