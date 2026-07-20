import { describe, expect, it } from 'vitest';

import type { WorkbenchMessage } from '../context/ApiContext';
import {
  activityDurationParts,
  activityRowFromMessage,
  filterActivityRows,
  genericChips,
  groupFromWire,
  initialLiveActivity,
  isActivityMessageType,
  liveActivityReducer,
  parseToolCall,
  parseToolName,
  shouldShowRunningCard,
  toolIconKind,
  toolParams,
  toolRecipe,
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

describe('parseToolCall (tier gating: extract name + JSON args, or null)', () => {
  it('extracts args from each backend shape', () => {
    // Claude (Capitalized, file_path)
    expect(parseToolCall('🔧 `Bash` `{"command":"pdftotext report.pdf"}`').args).toEqual({
      command: 'pdftotext report.pdf',
    });
    // Codex bundles result fields into bash args
    expect(
      parseToolCall('🔧 `bash` `{"command":"ls","status":"completed","exit_code":0,"output":"a"}`').args,
    ).toEqual({ command: 'ls', status: 'completed', exit_code: 0, output: 'a' });
    // OpenCode (lowercase, path)
    expect(parseToolCall('🔧 `read` `{"path":"foo.py"}`').args).toEqual({ path: 'foo.py' });
    expect(parseToolCall('🔧 `Bash` `{"command":"x"}`').name).toBe('Bash');
  });

  it('yields args:null (→ tier 3) for no-params / malformed / non-object / oversized / restore-path', () => {
    expect(parseToolCall('🔧 `TodoWrite`').args).toBeNull(); // no params
    expect(parseToolCall('🔧 `Bash` `{"command":}`').args).toBeNull(); // malformed JSON
    expect(parseToolCall('🔧 `Bash` `[1,2,3]`').args).toBeNull(); // JSON but not an object
    expect(parseToolCall('`bash`: `ls -la`').args).toBeNull(); // OpenCode restore path (no JSON)
    const huge = `🔧 \`Bash\` \`{"command":"${'x'.repeat(20001)}"}\``;
    expect(parseToolCall(huge).args).toBeNull(); // oversized → raw
  });

  it('never throws on arbitrary garbage (exception → tier 3)', () => {
    expect(() => parseToolCall('')).not.toThrow();
    expect(() => parseToolCall('not a tool call at all')).not.toThrow();
    expect(parseToolCall('🔧 `X` `{"a":"\\u00e4' /* truncated */).args).toBeNull();
  });
});

describe('toolRecipe (tier 1: known-tool recipes, backend-agnostic)', () => {
  it('bash/shell family → command (Claude/Codex/OpenCode)', () => {
    expect(toolRecipe('Bash', { command: 'pwd' })).toEqual({ kind: 'command', command: 'pwd' });
    expect(toolRecipe('bash', { command: 'ls', status: 'completed', exit_code: 0 })).toEqual({
      kind: 'command',
      command: 'ls',
    });
  });

  it('read/list family → dir muted + basename (probes file_path → path → file)', () => {
    expect(toolRecipe('Read', { file_path: '研报/中金_CATL_2026展望.pdf' })).toEqual({
      kind: 'read',
      dir: '研报/',
      base: '中金_CATL_2026展望.pdf',
    });
    expect(toolRecipe('read', { path: 'notes.md' })).toEqual({ kind: 'read', dir: '', base: 'notes.md' });
    expect(toolRecipe('LS', { path: '/tmp/dir/' })).toEqual({ kind: 'read', dir: '/tmp/', base: 'dir' });
  });

  it('edit/write/apply → fileop with an operation derived from name or Codex type', () => {
    expect(toolRecipe('Write', { file_path: 'new.txt', content: 'x' })).toEqual({
      kind: 'fileop',
      dir: '',
      base: 'new.txt',
      op: 'create',
    });
    expect(toolRecipe('Edit', { file_path: 'a.ts', old_string: 'a', new_string: 'b' })).toMatchObject({
      kind: 'fileop',
      op: 'modify',
    });
    expect(toolRecipe('file_change', { file: 'x.py', type: 'created' })).toMatchObject({ op: 'create' });
    expect(toolRecipe('file_change', { file: 'x.py', type: 'deleted' })).toMatchObject({ op: 'delete' });
    expect(toolRecipe('file_change', { file: 'x.py', type: 'modified' })).toMatchObject({ op: 'modify' });
  });

  it('web/search/fetch/grep → quoted query or URL', () => {
    expect(toolRecipe('WebSearch', { query: 'CATL 2026' })).toEqual({ kind: 'query', text: 'CATL 2026' });
    expect(toolRecipe('WebFetch', { url: 'https://x.com', prompt: 'p' })).toEqual({
      kind: 'query',
      text: 'https://x.com',
    });
    expect(toolRecipe('Grep', { pattern: 'consolidated' })).toEqual({ kind: 'query', text: 'consolidated' });
  });

  it('task/agent → description', () => {
    expect(toolRecipe('Task', { description: 'read two reports', prompt: '…' })).toEqual({
      kind: 'text',
      text: 'read two reports',
    });
  });

  it('returns null for unknown tools and for a known tool missing its primary arg (→ tier 2/3)', () => {
    expect(toolRecipe('custom_tool', { path: '/tmp/x.csv', rows: 1200 })).toBeNull();
    expect(toolRecipe('Bash', { description: 'no command here' })).toBeNull();
    expect(toolRecipe('Read', {})).toBeNull();
  });
});

describe('genericChips (tier 2: unknown tool, JSON parses)', () => {
  it('shows up to 3 short scalar params + an overflow count for the rest', () => {
    const { chips, overflow } = genericChips({
      path: '/tmp/x.csv',
      rows: 1200,
      format: 'csv',
      mode: 'r',
      note: 'extra',
    });
    expect(chips).toEqual([
      { key: 'path', value: '/tmp/x.csv' },
      { key: 'rows', value: '1200' },
      { key: 'format', value: 'csv' },
    ]);
    expect(overflow).toBe(2);
  });

  it('truncates long values and counts non-scalar params as overflow', () => {
    const { chips, overflow } = genericChips({ big: 'y'.repeat(100), obj: { a: 1 }, ok: 'v' });
    expect(chips.find((c) => c.key === 'big')?.value.endsWith('…')).toBe(true);
    expect(chips.some((c) => c.key === 'obj')).toBe(false); // object is not chip-worthy
    expect(overflow).toBe(1); // the object param
  });
});

describe('toolParams (D: inline detail kv table)', () => {
  it('orders kv, flags long/multiline as block, humanizes timeout ms', () => {
    const params = toolParams({
      command: 'pdftotext -layout report.pdf -',
      workdir: '.',
      timeout: 70000,
    });
    expect(params.map((p) => p.key)).toEqual(['command', 'workdir', 'timeout']);
    expect(params.find((p) => p.key === 'workdir')).toEqual({ key: 'workdir', value: '.', block: false });
    expect(params.find((p) => p.key === 'timeout')?.value).toBe('70000 (70s)');
    expect(toolParams({ patch: 'line1\nline2' })[0].block).toBe(true);
  });
});

describe('filterActivityRows (B: eye toggle filters tool rows only)', () => {
  const rows: ActivityRow[] = [
    { id: 'a1', kind: 'assistant', text: 'narration', created_at: 't1' },
    { id: 't1', kind: 'tool_call', text: '🔧 `Bash` `{"command":"ls"}`', created_at: 't2' },
    { id: 't2', kind: 'tool_call', text: '🔧 `Read` `{"file_path":"x"}`', created_at: 't3' },
  ];

  it('shows everything when tools are enabled', () => {
    expect(filterActivityRows(rows, true)).toEqual({ visible: rows, hiddenTools: 0 });
  });

  it('hides tool rows but keeps assistant narration, counting the hidden tools', () => {
    const { visible, hiddenTools } = filterActivityRows(rows, false);
    expect(visible.map((r) => r.id)).toEqual(['a1']);
    expect(hiddenTools).toBe(2);
  });

  it('everything filtered when there is no narration (drives the placeholder)', () => {
    const toolsOnly = rows.filter((r) => r.kind === 'tool_call');
    const { visible, hiddenTools } = filterActivityRows(toolsOnly, false);
    expect(visible).toEqual([]);
    expect(hiddenTools).toBe(2);
  });

  it('never hides assistant rows even with tools off', () => {
    const narrationOnly = rows.filter((r) => r.kind === 'assistant');
    expect(filterActivityRows(narrationOnly, false)).toEqual({ visible: narrationOnly, hiddenTools: 0 });
  });
});

describe('activityDurationParts', () => {
  it('splits into whole-second minutes/seconds (units applied via i18n)', () => {
    expect(activityDurationParts(45000)).toEqual({ minutes: 0, seconds: 45 });
    expect(activityDurationParts(83000)).toEqual({ minutes: 1, seconds: 23 });
    expect(activityDurationParts(600000)).toEqual({ minutes: 10, seconds: 0 });
  });

  it('returns null for null/negative', () => {
    expect(activityDurationParts(null)).toBeNull();
    expect(activityDurationParts(-5)).toBeNull();
  });
});

describe('shouldShowRunningCard', () => {
  const rows = [{ id: 'a', kind: 'tool_call', text: 'x', created_at: 't' }] as ActivityRow[];
  it('renders only while enabled AND working AND the buffer is non-empty', () => {
    expect(shouldShowRunningCard(true, true, rows.length)).toBe(true);
    expect(shouldShowRunningCard(false, true, rows.length)).toBe(false);
    expect(shouldShowRunningCard(true, true, 0)).toBe(false);
  });
  it('hides a stale buffer by construction once working goes false (R5: idle-recovered turn)', () => {
    // A dropped turn.end recovered by the idle poll clears ``working`` while the
    // buffer still holds the finished turn's rows — the card must not linger.
    expect(shouldShowRunningCard(true, false, rows.length)).toBe(false);
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
  it('maps snake_case wire fields (incl. anchor_position + open) to the group', () => {
    const group = groupFromWire({
      id: 'm_a1',
      anchor_message_id: 'm_r1',
      anchor_position: 'before',
      open: false,
      status: 'done',
      steps: 3,
      duration_ms: 83000,
      started_at: '2026-06-01T10:00:00Z',
      rows: [{ id: 'm_a1', kind: 'assistant', text: 'hi', created_at: '2026-06-01T10:00:01Z' }],
    });
    expect(group.anchorMessageId).toBe('m_r1');
    expect(group.anchorPosition).toBe('before');
    expect(group.open).toBe(false);
    expect(group.durationMs).toBe(83000);
    expect(group.rows).toHaveLength(1);
    expect(group.rows?.[0].kind).toBe('assistant');
  });

  it('maps an open interrupted group anchored after its trigger', () => {
    const group = groupFromWire({
      id: 'e_t1',
      anchor_message_id: 'm_u2',
      anchor_position: 'after',
      open: true,
      status: 'interrupted',
      steps: 1,
      duration_ms: null,
    });
    expect(group.anchorMessageId).toBe('m_u2');
    expect(group.anchorPosition).toBe('after');
    expect(group.open).toBe(true);
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
    const s = liveActivityReducer(initialLiveActivity(), { type: 'turn_start' });
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
