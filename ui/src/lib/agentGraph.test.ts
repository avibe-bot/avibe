import { describe, expect, it } from 'vitest';

import {
  type AgentGraphEdge,
  type AgentGraphNode,
  type AgentGraphTriggerNode,
  buildGraphForest,
  deriveLineage,
  formatElapsed,
  isBackground,
  nodeDisplayTitle,
  statusMeta,
} from './agentGraph';

function node(id: string, over: Partial<AgentGraphNode> = {}): AgentGraphNode {
  return {
    session_id: id,
    title: null,
    agent_name: 'claude',
    agent_backend: 'claude',
    model: null,
    reasoning_effort: null,
    status: 'idle',
    live: false,
    scope_id: null,
    project_id: null,
    scope_label: null,
    platform: null,
    workdir: null,
    openable_in_chat: true,
    created_at: '2026-07-23T00:00:00Z',
    last_active_at: '2026-07-23T00:00:00Z',
    elapsed_seconds: null,
    run_counts: { total: 0, running: 0 },
    ...over,
  };
}

const trigger: AgentGraphTriggerNode = {
  definition_id: 'def_1',
  definition_type: 'scheduled',
  name: 'Daily',
  schedule_label: 'cron 10:17',
  enabled: true,
};

describe('formatElapsed', () => {
  it('humanizes seconds/minutes/hours', () => {
    expect(formatElapsed(12)).toBe('12s');
    expect(formatElapsed(185)).toBe('3m');
    expect(formatElapsed(3700)).toBe('1h');
    expect(formatElapsed(null)).toBe('—');
    expect(formatElapsed(-5)).toBe('0s');
  });
});

describe('nodeDisplayTitle', () => {
  it('prefers the title, else agent + session suffix', () => {
    expect(nodeDisplayTitle(node('ses_abc', { title: 'Root' }))).toBe('Root');
    expect(nodeDisplayTitle(node('ses_123456', { title: null, agent_name: 'pm' }))).toBe('pm · 123456');
  });
});

describe('statusMeta / isBackground', () => {
  it('maps status to tone + glyph', () => {
    expect(statusMeta('active').glyph).toBe('dot');
    expect(statusMeta('succeeded').glyph).toBe('check');
    expect(statusMeta('failed').glyph).toBe('cross');
    expect(statusMeta('failed').tone).toBe('destructive');
  });
  it('treats absent visibility as foreground', () => {
    expect(isBackground(node('a'))).toBe(false);
    expect(isBackground(node('a', { visibility: 'background' }))).toBe(true);
  });
});

describe('deriveLineage', () => {
  const edges: AgentGraphEdge[] = [
    { kind: 'spawn', from: 'root', to: 'child', run_count: 1, last_at: '2026-07-23T01:00:00Z' },
    { kind: 'spawn', from: 'root2', to: 'child', run_count: 1, last_at: '2026-07-23T02:00:00Z' },
    { kind: 'callback', from: 'child', to: 'root', status: 'pending', last_at: '2026-07-23T02:00:00Z' },
    { kind: 'trigger', from: 'def:def_1', to: 'child', run_count: 3, last_at: '2026-07-23T00:30:00Z' },
  ];
  const triggersById = new Map([[trigger.definition_id, trigger]]);

  it('picks the latest spawn caller, the callback target+status, and the trigger', () => {
    const lineage = deriveLineage('child', edges, triggersById);
    expect(lineage.spawnedBy).toBe('root2'); // newest spawn edge in
    expect(lineage.callbackTo).toBe('root');
    expect(lineage.callbackStatus).toBe('pending');
    expect(lineage.trigger?.definition_id).toBe('def_1');
  });

  it('returns nulls for an unconnected node', () => {
    const lineage = deriveLineage('orphan', edges, triggersById);
    expect(lineage).toEqual({ spawnedBy: null, callbackTo: null, callbackStatus: null, trigger: null });
  });
});

describe('buildGraphForest', () => {
  it('nests children under their spawn parent with increasing depth', () => {
    const nodes = [node('root', { live: true }), node('a'), node('b')];
    const edges: AgentGraphEdge[] = [
      { kind: 'spawn', from: 'root', to: 'a' },
      { kind: 'spawn', from: 'a', to: 'b' },
    ];
    const rows = buildGraphForest(nodes, edges);
    expect(rows.map((r) => [r.node.session_id, r.depth])).toEqual([
      ['root', 0],
      ['a', 1],
      ['b', 2],
    ]);
  });

  it('attaches a trigger to its target session', () => {
    const nodes = [node('t')];
    const edges: AgentGraphEdge[] = [{ kind: 'trigger', from: 'def:def_1', to: 't' }];
    const rows = buildGraphForest(nodes, edges, [trigger]);
    expect(rows[0].trigger?.definition_id).toBe('def_1');
  });

  it('keeps the latest trigger per session by last_at', () => {
    const nodes = [node('t')];
    const tr2: AgentGraphTriggerNode = { ...trigger, definition_id: 'def_2', name: 'Newer' };
    const edges: AgentGraphEdge[] = [
      { kind: 'trigger', from: 'def:def_1', to: 't', last_at: '2026-07-23T00:00:00Z' },
      { kind: 'trigger', from: 'def:def_2', to: 't', last_at: '2026-07-23T05:00:00Z' },
    ];
    expect(buildGraphForest(nodes, edges, [trigger, tr2])[0].trigger?.definition_id).toBe('def_2');
    // Order-independent: the newest wins regardless of edge iteration order.
    expect(buildGraphForest(nodes, [...edges].reverse(), [trigger, tr2])[0].trigger?.definition_id).toBe('def_2');
  });

  it('guards cycles so every node appears exactly once', () => {
    const nodes = [node('x'), node('y')];
    const edges: AgentGraphEdge[] = [
      { kind: 'spawn', from: 'x', to: 'y' },
      { kind: 'spawn', from: 'y', to: 'x' },
    ];
    const rows = buildGraphForest(nodes, edges);
    expect(rows).toHaveLength(2);
    expect(new Set(rows.map((r) => r.node.session_id))).toEqual(new Set(['x', 'y']));
  });

  it('orders roots live-first', () => {
    const nodes = [node('old', { live: false }), node('hot', { live: true })];
    const rows = buildGraphForest(nodes, []);
    expect(rows[0].node.session_id).toBe('hot');
  });
});
