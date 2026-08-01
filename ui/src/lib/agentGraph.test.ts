import { describe, expect, it } from 'vitest';

import en from '../i18n/en.json';
import zh from '../i18n/zh.json';
import {
  type AgentGraphEdge,
  type AgentGraphNode,
  type AgentGraphTriggerNode,
  buildGraphForest,
  computeFillHeight,
  deriveLineage,
  filterDisabledTriggers,
  formatElapsed,
  isBackground,
  nodeDisplayTitle,
  statusMeta,
  triggerFiredSessionIds,
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

// Resolves against a real bundle: the unit is user-visible copy composed into
// the value, so a test that stubbed it would assert the formatter agrees with
// itself rather than with what ships.
const unit = (bundle: { common: { duration: Record<string, string> } }) => (k: string) =>
  bundle.common.duration[k.replace('common.duration.', '')];
const t = unit(en);

describe('formatElapsed', () => {
  it('humanizes seconds/minutes/hours', () => {
    expect(formatElapsed(12, t)).toBe('12s');
    expect(formatElapsed(185, t)).toBe('3m');
    expect(formatElapsed(3700, t)).toBe('1h');
    expect(formatElapsed(null, t)).toBe('—');
    expect(formatElapsed(-5, t)).toBe('0s');
  });

  it('switches to days rather than counting past 24 hours', () => {
    // A Harness watch waits for days on end; "168h" is a number the reader has
    // to divide before it means anything.
    expect(formatElapsed(86_399, t)).toBe('23h');
    expect(formatElapsed(86_400, t)).toBe('1d');
    expect(formatElapsed(7 * 86_400 + 3600, t)).toBe('7d');
  });

  it('takes every unit from the locale instead of hardcoding English', () => {
    // The whole formatter, not just the day branch: "等待 3h" was already
    // shipping before the day unit was added, so fixing one suffix would have
    // left the same defect in the other three.
    const zhT = unit(zh);
    expect(formatElapsed(12, zhT)).toBe('12秒');
    expect(formatElapsed(185, zhT)).toBe('3分');
    expect(formatElapsed(3700, zhT)).toBe('1小时');
    expect(formatElapsed(3 * 86_400, zhT)).toBe('3天');
  });
});

describe('nodeDisplayTitle', () => {
  it('prefers the title, else agent + session suffix', () => {
    expect(nodeDisplayTitle(node('ses_abc', { title: 'Root' }))).toBe('Root');
    expect(nodeDisplayTitle(node('ses_123456', { title: null, agent_name: 'pm' }))).toBe('pm · 123456');
    expect(nodeDisplayTitle(node('ses_123456', {
      title: null,
      agent_name: '_pm-8dd7',
      agent_display_name: 'pm',
    }))).toBe('pm · 123456');
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

describe('filterDisabledTriggers (A11 hide-disabled)', () => {
  const enabledTr: AgentGraphTriggerNode = { ...trigger, definition_id: 'def_on', enabled: true };
  const disabledTr: AgentGraphTriggerNode = { ...trigger, definition_id: 'def_off', enabled: false };
  const edges: AgentGraphEdge[] = [
    { kind: 'spawn', from: 'root', to: 'child' },
    { kind: 'trigger', from: 'def:def_on', to: 'a' },
    { kind: 'trigger', from: 'def:def_off', to: 'b' },
  ];

  it('passes the same array refs through untouched when showDisabled is on', () => {
    const triggers = [enabledTr, disabledTr];
    const out = filterDisabledTriggers(triggers, edges, true);
    expect(out.triggerNodes).toBe(triggers); // same reference — no needless re-layout
    expect(out.edges).toBe(edges);
  });

  it('drops disabled chips and only their trigger edges when off', () => {
    const out = filterDisabledTriggers([enabledTr, disabledTr], edges, false);
    expect(out.triggerNodes).toEqual([enabledTr]);
    // The disabled def's trigger edge is gone; the spawn edge and the enabled
    // def's trigger edge survive.
    expect(out.edges).toEqual([
      { kind: 'spawn', from: 'root', to: 'child' },
      { kind: 'trigger', from: 'def:def_on', to: 'a' },
    ]);
  });

  it('returns inputs by reference when nothing is disabled', () => {
    const only = [enabledTr];
    const out = filterDisabledTriggers(only, edges, false);
    expect(out.triggerNodes).toBe(only);
    expect(out.edges).toBe(edges);
  });
});

describe('triggerFiredSessionIds', () => {
  const edges: AgentGraphEdge[] = [
    { kind: 'trigger', from: 'def:def_1', to: 'ses_old', last_at: '2026-07-23T01:00:00Z' },
    { kind: 'trigger', from: 'def:def_1', to: 'ses_new', last_at: '2026-07-23T05:00:00Z' },
    // A repeat edge for the same session keeps the newest last_at, not a dupe.
    { kind: 'trigger', from: 'def:def_1', to: 'ses_old', last_at: '2026-07-23T02:00:00Z' },
    { kind: 'trigger', from: 'def:other', to: 'ses_x', last_at: '2026-07-23T09:00:00Z' },
    { kind: 'spawn', from: 'def:def_1', to: 'ses_y' }, // non-trigger edge ignored
  ];

  it('returns the def’s fired sessions newest-first, de-duplicated', () => {
    expect(triggerFiredSessionIds('def_1', edges)).toEqual(['ses_new', 'ses_old']);
  });

  it('returns empty for a definition that fired nothing in-window', () => {
    expect(triggerFiredSessionIds('def_absent', edges)).toEqual([]);
  });
});

describe('computeFillHeight (desktop canvas fill height)', () => {
  it('fills the viewport below the graph top, minus the bottom gap', () => {
    // 900 tall, graph starts 260 down, 24 gap ⇒ 616.
    expect(computeFillHeight(900, 260, 24, 480)).toBe(616);
  });

  it('floors at the minimum when the window is too short to fit it', () => {
    // Only 200px would remain — clamp up so the page scrolls instead of crushing.
    expect(computeFillHeight(600, 376, 24, 480)).toBe(480);
  });

  it('rounds sub-pixel measurements to a whole number', () => {
    expect(computeFillHeight(900, 260.4, 24, 480)).toBe(616); // 615.6 → 616
  });
});
