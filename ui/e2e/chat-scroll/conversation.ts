// A long conversation whose rows differ in height the way a real chat's do:
// one-line asks, markdown replies with lists, code, tables, images that size
// themselves when they load, Harness callbacks, and execution-process chips.
// Deterministic by index, so every run scrolls the same document.
import type { WorkbenchMessage } from '../../src/context/ApiContext';
import type { TurnActivityGroupWire } from '../../src/lib/agentActivity';

export const LAB_SESSION_ID = 'sesscrolllab01';
const START = Date.UTC(2026, 9, 1, 8, 0);

const asks = [
  'Can you summarize what changed in the release notes?',
  'Keep going.',
  'Check the build on the phone too, and tell me what you find before you change anything.',
  'Looks good. Open the PR.',
  'What does the log say about the failed step?',
];

const replies = [
  (n: number) =>
    `Here is the summary for step ${n}:\n\n- The parser now accepts trailing commas.\n- Two flaky tests were rewritten.\n- The settings page loads its data once.\n- Error messages name the field that failed.\n- The changelog lists every user-facing change.`,
  (n: number) =>
    `The change for step ${n} is small:\n\n\`\`\`ts\nexport function parseList(input: string): string[] {\n  return input\n    .split(',')\n    .map((item) => item.trim())\n    .filter((item) => item.length > 0);\n}\n\nconst items = parseList('a, b, c,');\nconsole.log(items.length);\n\`\`\`\n\nIt ignores the empty item after the last comma.`,
  (n: number) =>
    `Inventory for batch ${n}:\n\n| Item | Count | Price |\n| --- | --- | --- |\n| Cables | 12 | 4.50 |\n| Adapters | 7 | 9.90 |\n| Chargers | 3 | 19.00 |\n| Cases | 21 | 6.25 |`,
  (n: number) => `The chart for week ${n}:\n\n![chart ${n}](/api/media/lab-img-${n})\n\nThe peak is the day the new version shipped.`,
  (n: number) =>
    `Long answer ${n}. ${'The release went out in three stages, and each stage waited for the previous one to report healthy before it started, so a problem in the first group would have stopped the rollout before it reached everyone else. '.repeat(4)}`,
  (n: number) => `Done (${n}).`,
];

function row(index: number, extra: Partial<WorkbenchMessage>): WorkbenchMessage {
  const at = new Date(START + index * 60_000).toISOString();
  return {
    id: `msg_${String(index + 1).padStart(5, '0')}`,
    scope_id: 'avibe::project::proj_avibe',
    session_id: LAB_SESSION_ID,
    platform: 'avibe',
    author: 'user',
    type: 'user',
    source: 'user',
    author_id: 'local',
    author_name: null,
    native_message_id: null,
    parent_native_message_id: null,
    text: '',
    content: {},
    metadata: {},
    created_at: at,
    updated_at: at,
    delivered_at: at,
    read_at: at,
    ...extra,
  } as WorkbenchMessage;
}

export function messageAt(index: number): WorkbenchMessage {
  // Every 23rd row is a Harness callback, as long-running work reports in, and
  // four in a row near the end fold into a Harness group.
  if (index % 23 === 11 || (index >= 372 && index <= 375)) {
    return row(index, {
      author: 'harness',
      type: 'harness',
      source: 'harness',
      author_id: null,
      author_name: 'agent_run',
      text: `Progress ${index}: the nightly build passed; the release check is next.`,
      source_session_id: 'seslane0001',
      source_session_title: 'Release helper',
    } as Partial<WorkbenchMessage>);
  }
  if (index % 2 === 0) return row(index, { text: `${asks[(index / 2) % asks.length]} (#${index + 1})` });
  return row(index, {
    author: 'agent',
    type: 'result',
    source: 'agent',
    author_id: null,
    text: replies[Math.floor(index / 2) % replies.length](index + 1),
  });
}

export function conversation(count: number): WorkbenchMessage[] {
  return Array.from({ length: count }, (_, index) => messageAt(index));
}

type ActivityRowWire = NonNullable<TurnActivityGroupWire['rows']>[number];

/** The steps a settled turn took, as its group's rows load: narration, tool calls, and a long output. */
export function activityRecords(group: TurnActivityGroupWire): ActivityRowWire[] {
  const at = Date.parse(group.started_at ?? '2026-10-01T08:00:00Z');
  const row = (index: number, kind: ActivityRowWire['kind'], text: string): ActivityRowWire => ({
    id: `evt_${group.id}_${index}`,
    kind,
    text,
    created_at: new Date(at + index * 3_000).toISOString(),
  });
  const tool = (index: number, name: string, args: Record<string, unknown>) => row(index, 'tool_call', `🔧 \`${name}\` \`${JSON.stringify(args)}\``);
  return [
    row(0, 'assistant', 'Read the failed step of the nightly build first.'),
    tool(1, 'Bash', { command: 'gh run view 4231 --log-failed | tail -80', description: 'Read the failed step' }),
    tool(2, 'Read', { file_path: '/Users/demo/avibe/ui/src/lib/parseList.ts' }),
    tool(3, 'Grep', { pattern: 'parseList\\(', path: 'ui/src' }),
    row(4, 'assistant', 'The `parseList` test failed:\n\n```\nFAIL  src/lib/parseList.test.ts > ignores a trailing comma\n  AssertionError: expected [ \'a\', \'b\', \'c\', \'\' ]\n  to deeply equal [ \'a\', \'b\', \'c\' ]\n```\n\nThe split keeps the empty item.'),
    tool(5, 'Edit', { file_path: '/Users/demo/avibe/ui/src/lib/parseList.ts' }),
    tool(6, 'Bash', { command: 'npx vitest run src/lib/parseList.test.ts', description: 'Run the one test' }),
    row(7, 'assistant', 'It passes. Run the unit suite once more.'),
    tool(8, 'Bash', { command: 'npx vitest run src/lib', description: 'Unit suite' }),
    row(9, 'assistant', 'All 1841 tests pass.'),
  ];
}

/** A settled execution-process group above every third reply; `records`: with the steps it took (else it
 *  counts four and has none). */
export function activityGroups(messages: WorkbenchMessage[], records = false): TurnActivityGroupWire[] {
  return messages
    .filter((message, index) => message.type === 'result' && index % 3 === 1)
    .map((message) => ({
      id: `grp_${message.id}`,
      anchor_message_id: message.id,
      anchor_position: 'before',
      open: false,
      status: 'done',
      steps: records ? 10 : 4,
      duration_ms: 42_000,
      started_at: message.created_at,
      rows: [],
    }) as TurnActivityGroupWire);
}
