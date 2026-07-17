import { describe, expect, it } from 'vitest';

import type { WorkbenchMessage } from '../context/ApiContext';
import {
  activityRowFromMessage,
  formatActivityDuration,
  groupFromWire,
  initialLiveActivity,
  isActivityMessageType,
  liveActivityReducer,
  parseToolName,
  toolIconKind,
  toolSummary,
  type ActivityRow,
} from './agentActivity';

// ``format_toolcall`` stores one string: "🔧 `ToolName` `{json params}`".
const BASH = '🔧 `Bash` `{"command":"pdftotext report.pdf"}`';
const READ = '🔧 `Read` `{"path":"notes.md"}`';
const NO_PARAMS = '🔧 `TodoWrite`';

describe('parseToolName', () => {
  it('extracts the backtick-wrapped tool name after the wrench', () => {
    expect(parseToolName(BASH)).toBe('Bash');
    expect(parseToolName(READ)).toBe('Read');
    expect(parseToolName(NO_PARAMS)).toBe('TodoWrite');
  });

  it('falls back to the first token when there is no backtick', () => {
    expect(parseToolName('🔧 WebSearch results')).toBe('WebSearch');
    expect(parseToolName('')).toBe('');
  });
});

describe('toolSummary', () => {
  it('returns the first-line remainder after the tool name, unwrapped', () => {
    expect(toolSummary(BASH)).toBe('{"command":"pdftotext report.pdf"}');
    expect(toolSummary(NO_PARAMS)).toBe('');
  });

  it('keeps only the first line', () => {
    expect(toolSummary('🔧 `Bash` `ls`\nsecond line')).toBe('ls');
  });
});

describe('toolIconKind', () => {
  it('maps tool-name families to a stable icon key', () => {
    expect(toolIconKind('Bash')).toBe('terminal');
    expect(toolIconKind('Read')).toBe('file');
    expect(toolIconKind('Edit')).toBe('edit');
    expect(toolIconKind('Write')).toBe('edit');
    expect(toolIconKind('WebSearch')).toBe('web');
    expect(toolIconKind('Task')).toBe('agent');
    expect(toolIconKind('SomethingElse')).toBe('wrench');
  });
});

describe('formatActivityDuration', () => {
  it('formats sub-minute durations in seconds', () => {
    expect(formatActivityDuration(3200)).toBe('3.2s');
    expect(formatActivityDuration(1800)).toBe('1.8s');
    expect(formatActivityDuration(12000)).toBe('12s');
  });

  it('formats minute-plus durations as "Xm Ys"', () => {
    expect(formatActivityDuration(83000)).toBe('1m 23s');
    expect(formatActivityDuration(600000)).toBe('10m 0s');
  });

  it('returns empty string for null/negative', () => {
    expect(formatActivityDuration(null)).toBe('');
    expect(formatActivityDuration(-5)).toBe('');
  });
});

describe('isActivityMessageType', () => {
  it('is true only for assistant + tool_call', () => {
    expect(isActivityMessageType('assistant')).toBe(true);
    expect(isActivityMessageType('tool_call')).toBe(true);
    expect(isActivityMessageType('result')).toBe(false);
    expect(isActivityMessageType('user')).toBe(false);
  });
});

describe('groupFromWire', () => {
  it('maps snake_case wire fields to the camelCase group', () => {
    const group = groupFromWire({
      id: 'm_a1',
      anchor_message_id: 'm_r1',
      status: 'done',
      steps: 3,
      duration_ms: 83000,
      started_at: '2026-06-01T10:00:00Z',
      rows: [{ id: 'm_a1', kind: 'assistant', text: 'hi', created_at: '2026-06-01T10:00:01Z' }],
    });
    expect(group.anchorMessageId).toBe('m_r1');
    expect(group.durationMs).toBe(83000);
    expect(group.rows).toHaveLength(1);
    expect(group.rows?.[0].kind).toBe('assistant');
  });

  it('normalizes null anchor + missing duration/rows', () => {
    const group = groupFromWire({
      id: 'e_t1',
      anchor_message_id: null,
      status: 'interrupted',
      steps: 1,
      duration_ms: null,
    });
    expect(group.anchorMessageId).toBeNull();
    expect(group.durationMs).toBeNull();
    expect(group.rows).toBeUndefined();
  });
});

describe('activityRowFromMessage', () => {
  it('derives kind from the message type', () => {
    const assistant = activityRowFromMessage({ id: 'm1', type: 'assistant', text: 'thinking', created_at: 't1' } as WorkbenchMessage);
    expect(assistant).toEqual({ id: 'm1', kind: 'assistant', text: 'thinking', created_at: 't1' });
    const tool = activityRowFromMessage({ id: 'e1', type: 'tool_call', text: '🔧 `Bash`', created_at: 't2' } as WorkbenchMessage);
    expect(tool.kind).toBe('tool_call');
  });
});

describe('liveActivityReducer (generation invariant)', () => {
  const row = (id: string): ActivityRow => ({ id, kind: 'tool_call', text: id, created_at: `t-${id}` });

  it('turn_start bumps the generation and clears the buffer', () => {
    let s = initialLiveActivity();
    s = liveActivityReducer(s, { type: 'row', row: row('a'), now: 1 });
    s = liveActivityReducer(s, { type: 'turn_start' });
    expect(s.gen).toBe(1);
    expect(s.rows).toEqual([]);
    expect(s.startedAt).toBeNull();
  });

  it('rows append within a generation; the first stamps startedAt', () => {
    let s = liveActivityReducer(initialLiveActivity(), { type: 'turn_start' });
    s = liveActivityReducer(s, { type: 'row', row: row('a'), now: 100 });
    s = liveActivityReducer(s, { type: 'row', row: row('b'), now: 200 });
    expect(s.rows.map((r) => r.id)).toEqual(['a', 'b']);
    expect(s.startedAt).toBe(100); // unchanged by the second row
  });

  it('a row after settle opens a new agent-initiated generation', () => {
    let s = liveActivityReducer(initialLiveActivity(), { type: 'turn_start' });
    s = liveActivityReducer(s, { type: 'row', row: row('a'), now: 1 });
    s = liveActivityReducer(s, { type: 'settle' });
    const genAfterSettle = s.gen;
    s = liveActivityReducer(s, { type: 'row', row: row('b'), now: 5 });
    expect(s.gen).toBe(genAfterSettle + 1);
    expect(s.rows.map((r) => r.id)).toEqual(['b']); // fresh buffer, not merged with 'a'
    expect(s.settled).toBe(false);
  });

  it('clear_for_gen only clears its own generation — a late refresh after the next turn is a no-op (#499)', () => {
    let s = liveActivityReducer(initialLiveActivity(), { type: 'turn_start' }); // gen 1
    s = liveActivityReducer(s, { type: 'row', row: row('a'), now: 1 });
    const gen1 = s.gen;
    s = liveActivityReducer(s, { type: 'settle' });
    s = liveActivityReducer(s, { type: 'turn_start' }); // gen 2, buffer cleared
    s = liveActivityReducer(s, { type: 'row', row: row('b'), now: 2 }); // gen 2's live row
    // The gen-1 settle refresh resolves LATE:
    const after = liveActivityReducer(s, { type: 'clear_for_gen', gen: gen1 });
    expect(after.rows.map((r) => r.id)).toEqual(['b']); // gen 2's live row is NOT wiped
    // The current-gen clear does clear it:
    const cleared = liveActivityReducer(s, { type: 'clear_for_gen', gen: s.gen });
    expect(cleared.rows).toEqual([]);
  });

  it('rehydrate_for_gen fills only an empty buffer of the current generation', () => {
    let s = liveActivityReducer(initialLiveActivity(), { type: 'turn_start' });
    const gen = s.gen;
    const hydrated = liveActivityReducer(s, {
      type: 'rehydrate_for_gen',
      gen,
      rows: [row('x'), row('y')],
      startedAt: 50,
    });
    expect(hydrated.rows.map((r) => r.id)).toEqual(['x', 'y']);
    // Does not clobber an already-filled buffer, nor a stale generation:
    const withLive = liveActivityReducer(hydrated, { type: 'row', row: row('z'), now: 60 });
    const noClobber = liveActivityReducer(withLive, { type: 'rehydrate_for_gen', gen, rows: [row('w')], startedAt: 70 });
    expect(noClobber.rows.map((r) => r.id)).toEqual(['x', 'y', 'z']);
    const staleGen = liveActivityReducer(hydrated, { type: 'rehydrate_for_gen', gen: gen - 1, rows: [row('w')], startedAt: 70 });
    expect(staleGen.rows.map((r) => r.id)).toEqual(['x', 'y']);
  });
});
