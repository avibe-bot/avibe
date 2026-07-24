// Pure matcher for the run-graph node search (M8). Kept out of the toolbar
// component so the match + ranking is unit-tested without React Flow.
//
// Nodes match on session_id (prefix or substring) or title substring; trigger
// chips match on their definition name (or definition_id substring). All
// case-insensitive. Ranking: an id prefix is the strongest signal, then a
// title/name substring, then an id substring — so "ses_ab…" lands the exact
// session first and a word in a title still surfaces its node.
import type { AgentGraphNode, AgentGraphTriggerNode } from './agentGraph';

export type GraphSearchResult =
  | { kind: 'node'; id: string; node: AgentGraphNode; rank: number }
  | { kind: 'trigger'; id: string; trigger: AgentGraphTriggerNode; rank: number };

// Ranks (lower = better).
const RANK_ID_PREFIX = 0;
const RANK_NAME_SUBSTR = 1;
const RANK_ID_SUBSTR = 2;

export function searchGraph(
  query: string,
  nodes: readonly AgentGraphNode[],
  triggers: readonly AgentGraphTriggerNode[],
): GraphSearchResult[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];

  const out: GraphSearchResult[] = [];

  nodes.forEach((node) => {
    const sid = node.session_id.toLowerCase();
    const title = (node.title ?? '').toLowerCase();
    let rank = -1;
    if (sid.startsWith(q)) rank = RANK_ID_PREFIX;
    else if (title.includes(q)) rank = RANK_NAME_SUBSTR;
    else if (sid.includes(q)) rank = RANK_ID_SUBSTR;
    if (rank >= 0) out.push({ kind: 'node', id: node.session_id, node, rank });
  });

  triggers.forEach((trigger) => {
    const name = (trigger.name ?? '').toLowerCase();
    const did = trigger.definition_id.toLowerCase();
    let rank = -1;
    if (name.includes(q)) rank = RANK_NAME_SUBSTR; // by definition name (spec)
    else if (did.includes(q)) rank = RANK_ID_SUBSTR; // id parity with nodes
    if (rank >= 0) out.push({ kind: 'trigger', id: trigger.definition_id, trigger, rank });
  });

  // Stable sort by rank; ties keep discovery order (nodes before triggers, then
  // payload order) via the index tiebreak, so results are deterministic.
  return out
    .map((r, i) => ({ r, i }))
    .sort((a, b) => a.r.rank - b.r.rank || a.i - b.i)
    .map((x) => x.r);
}
