// The lab's conversation: every disclosure the transcript has, in a conversation long enough to scroll.
// Execution-process chips with records (several steps, tool calls, long output), with none, and cut
// short; the running card at the tail; single Harness messages (long, so they fold); a Harness group of
// five, near the end and again higher up; long answers, tables, code, lists, and short asks between.
// Deterministic, with times ending now.
import type { WorkbenchMessage } from '../../src/context/ApiContext';
import type { TurnActivityGroupWire } from '../../src/lib/agentActivity';

export const LAB_SESSION_ID = 'seslabdisclose';
type Row = NonNullable<TurnActivityGroupWire['rows']>[number];

const STEP_MS = 75_000;

function base(id: string, at: string, extra: Partial<WorkbenchMessage>): WorkbenchMessage {
  return {
    id,
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

const user = (text: string) => ({ author: 'user', type: 'user', source: 'user', author_id: 'local', text }) as Partial<WorkbenchMessage>;
const agent = (text: string) => ({ author: 'agent', type: 'result', source: 'agent', author_id: null, text }) as Partial<WorkbenchMessage>;
const harness = (text: string, title = 'Release helper') => ({
  author: 'harness',
  type: 'harness',
  source: 'harness',
  author_id: null,
  author_name: 'agent_run',
  text,
  source_session_id: 'seslabhelper01',
  source_session_title: title,
}) as Partial<WorkbenchMessage>;

// ----- What the agent says -----

const longAnswer = (n: number) =>
  `第 ${n} 轮的结论先说：可以发，但要先关掉灰度开关。\n\n这次发布分三批推进，每一批都要等上一批报告健康之后才开始。第一批只覆盖内部账号，第二批扩到 5% 的用户，第三批全量。这样如果第一批出了问题，发布会在影响到其他人之前停下来。\n\n我对比了三批之间的错误率和延迟：错误率一直在 0.1% 以下，P95 延迟在第二批短暂上升到 420ms，原因是缓存预热，十分钟后回落到 180ms。\n\n剩下的风险只有一个：旧版客户端在断线重连时会重复拉取一次历史，量不大，但会放大第一分钟的读压力。建议在发布说明里提一句，并把重连退避从 1s 调到 3s。`;

const table = (n: number) =>
  `批次 ${n} 的对比结果：\n\n| 指标 | 上一版 | 这一版 | 变化 |\n| --- | --- | --- | --- |\n| 冷启动 | 1.84s | 1.21s | −34% |\n| 首屏渲染 | 920ms | 610ms | −34% |\n| 内存峰值 | 412MB | 388MB | −6% |\n| 包体积 | 3.2MB | 3.4MB | +6% |\n| 崩溃率 | 0.08% | 0.05% | −38% |\n\n包体积涨了一点，是因为新加了离线字体；其余都在变好。`;

const code = (n: number) =>
  `改动在 \`parseList\`（第 ${n} 步）：\n\n\`\`\`ts\nexport function parseList(input: string): string[] {\n  return input\n    .split(',')\n    .map((item) => item.trim())\n    .filter((item) => item.length > 0);\n}\n\nconst items = parseList('a, b, c,');\nconsole.log(items.length); // 3\n\`\`\`\n\n最后一个逗号后面的空项会被忽略，原来的实现会多出一个空字符串。`;

const list = (n: number) =>
  `第 ${n} 次整理的变更：\n\n- 解析器现在接受末尾逗号。\n- 两个不稳定的测试重写了，不再依赖固定等待。\n- 设置页只加载一次数据。\n- 错误提示会写出是哪个字段出错。\n- 更新日志列出了每一处用户能看到的改动。`;

const short = (n: number) => [`好了，已经推上去了（${n}）。`, `改完了，测试全过。`, `收到，按这个来。`, `已经合并。`][n % 4];

// ----- What the execution process holds -----

let rowSeq = 0;
const activityRow = (kind: Row['kind'], text: string, at: number): Row => {
  rowSeq += 1;
  return { id: `evt_lab_${rowSeq.toString(36).padStart(4, '0')}`, kind, text, created_at: new Date(at).toISOString() };
};
const narration = (text: string, at: number) => activityRow('assistant', text, at);
const tool = (name: string, args: Record<string, unknown>, at: number) => activityRow('tool_call', `🔧 \`${name}\` \`${JSON.stringify(args)}\``, at);

/** Several steps: tool calls, the agent's words between them, and a long piece of output. */
function recordRows(at: number, n: number): Row[] {
  const t = (i: number) => at - (12 - i) * 4_000;
  return [
    narration(`先看第 ${n} 次构建的日志，找到失败的那一步。`, t(0)),
    tool('Bash', { command: 'gh run view 4231 --log-failed | tail -80', description: '读失败步骤的日志' }, t(1)),
    tool('Read', { file_path: '/Users/demo/avibe/ui/src/lib/parseList.ts' }, t(2)),
    tool('Grep', { pattern: 'parseList\\(', path: 'ui/src', output_mode: 'content' }, t(3)),
    narration(
      `失败的是 \`parseList\` 的单测，输出如下：\n\n\`\`\`\nFAIL  src/lib/parseList.test.ts > parseList > ignores a trailing comma\n  AssertionError: expected [ 'a', 'b', 'c', '' ] to deeply equal [ 'a', 'b', 'c' ]\n    - Expected\n    + Received\n      [\n        "a",\n        "b",\n        "c",\n    +   "",\n      ]\n  ❯ src/lib/parseList.test.ts:14:32\n\nTest Files  1 failed | 211 passed (212)\n     Tests  1 failed | 1840 passed (1841)\n\`\`\`\n\n原因是 split 之后没有过滤空项。`,
      t(4),
    ),
    tool('Edit', { file_path: '/Users/demo/avibe/ui/src/lib/parseList.ts', old_string: '.map((item) => item.trim())', new_string: '.map((item) => item.trim())\n    .filter((item) => item.length > 0)' }, t(5)),
    tool('Bash', { command: 'npx vitest run src/lib/parseList.test.ts', description: '重跑这一个测试' }, t(6)),
    narration('通过了。再跑一遍整个单元测试，确认没有别的地方依赖那个空项。', t(7)),
    tool('Bash', { command: 'npx vitest run src/components src/context src/lib', description: '全量单元测试' }, t(8)),
    narration('1841 个测试全部通过。', t(9)),
  ];
}

/** A cut-short turn: a few steps, then the reader stopped it. */
const cutRows = (at: number) => [
  narration('找一下上周的发布记录。', at - 9_000),
  tool('Glob', { pattern: 'docs/releases/2026-10-*.md' }, at - 7_000),
  tool('WebSearch', { query: 'avibe release notes October 2026' }, at - 5_000),
];

/** The running turn's steps so far. */
export const runningRows = (at: number): Row[] => [
  narration('先读一下现在的展开动效是怎么实现的。', at - 20_000),
  tool('Read', { file_path: '/Users/demo/avibe/ui/src/lib/transcriptMotion.ts' }, at - 16_000),
  tool('Grep', { pattern: 'useDisclosure', path: 'ui/src', output_mode: 'files_with_matches' }, at - 12_000),
  narration('展开时内容一帧到位，然后淡入；下面的行直接跳下去。我先做几个变体对比。', at - 6_000),
];

/** A step the running turn takes next (the lab streams these while a turn runs). */
export const liveStep = (index: number): Partial<WorkbenchMessage> => {
  const steps: Partial<WorkbenchMessage>[] = [
    { type: 'tool_call', text: '🔧 `Read` `{"file_path":"/Users/demo/avibe/ui/src/index.css"}`' },
    { type: 'assistant', text: '动效 token 都在 index.css 里，展开用的是 fast（160ms）。' },
    { type: 'tool_call', text: '🔧 `Bash` `{"command":"npm run build","description":"构建一次"}`' },
    { type: 'tool_call', text: '🔧 `Edit` `{"file_path":"/Users/demo/avibe/ui/src/lib/transcriptMotion.ts"}`' },
    { type: 'assistant', text: '构建通过。接下来逐帧看一遍亮色和暗色。' },
    { type: 'tool_call', text: '🔧 `Bash` `{"command":"node record.mjs --theme dark","description":"录一段暗色"}`' },
  ];
  return { ...steps[index % steps.length], author: 'agent', source: 'agent', author_id: null };
};

const asks = [
  '帮我看看 CI 为什么挂了。',
  '继续。',
  '先在手机上也验证一下，有问题先告诉我，别直接改。',
  '看起来没问题，开 PR 吧。',
  '日志里关于失败那一步是怎么说的？',
  '把这次发布的风险整理一下，越具体越好，尤其是旧版客户端那部分。',
  '对比一下这一版和上一版的性能。',
  '好。',
];

const harnessLong = (n: number) =>
  `Release helper 第 ${n} 次汇报：夜间构建通过，发布检查进行到第二步。\n\n已完成：\n- 构建产物签名校验通过；\n- 安装/升级回归在 3 个系统版本上通过；\n- 迁移脚本在副本库上演练了一遍，耗时 41s，没有锁表。\n\n下一步：等第二批灰度的健康报告（大约 20 分钟），然后开始第三批。如果中途有告警，我会先暂停发布再来找你。`;

const harnessShort = (n: number, i: number) =>
  [
    `第 ${n} 批灰度开始，覆盖 5% 的用户。`,
    `第 ${n} 批健康检查通过：错误率 0.04%，P95 延迟 182ms。`,
    `第 ${n} 批扩到 20%，指标平稳。`,
    `第 ${n} 批全量完成，发布结束。`,
    `已经把发布记录写进 docs/releases/，并更新了状态页。`,
  ][i % 5];

// ----- The conversation -----

type Turn =
  | { kind: 'ask'; text: string }
  | { kind: 'reply'; text: string; activity?: 'records' | 'empty' }
  | { kind: 'cut'; text: string }
  | { kind: 'harness'; text: string }
  | { kind: 'group'; texts: string[] };

function script(): Turn[] {
  const turns: Turn[] = [];
  const replies = [longAnswer, table, code, list, short];
  for (let n = 1; n <= 22; n += 1) {
    turns.push({ kind: 'ask', text: asks[n % asks.length] });
    const reply = replies[n % replies.length](n);
    turns.push({ kind: 'reply', text: reply, activity: n % 3 === 0 ? 'records' : n % 3 === 1 ? 'empty' : undefined });
    if (n % 5 === 2) turns.push({ kind: 'harness', text: harnessLong(n) });
    if (n === 9) turns.push({ kind: 'group', texts: [0, 1, 2, 3, 4].map((i) => harnessShort(n, i)) });
    if (n === 13) turns.push({ kind: 'cut', text: '先停一下，我换个思路。' });
  }
  // The last screens: each disclosure once more, close together.
  turns.push(
    { kind: 'ask', text: '帮我看看 CI 为什么挂了。' },
    { kind: 'reply', text: longAnswer(23), activity: 'records' },
    { kind: 'group', texts: [0, 1, 2, 3, 4].map((i) => harnessShort(25, i)) },
    { kind: 'ask', text: '对比一下这一版和上一版的性能。' },
    { kind: 'reply', text: table(26), activity: 'empty' },
    { kind: 'harness', text: harnessLong(27) },
    { kind: 'ask', text: '那就先发第一批。' },
    { kind: 'reply', text: '好了，第一批已经开始，覆盖内部账号。', activity: 'records' },
  );
  return turns;
}

export type LabConversation = {
  history: WorkbenchMessage[];
  groups: TurnActivityGroupWire[];
  /** The ask the running turn answers, at the tail. */
  runningAsk: WorkbenchMessage;
};

export function labConversation(now = Date.now()): LabConversation {
  const turns = script();
  const rows = turns.reduce((total, turn) => total + (turn.kind === 'group' ? turn.texts.length : turn.kind === 'cut' ? 2 : 1), 0) + 1;
  const history: WorkbenchMessage[] = [];
  const groups: TurnActivityGroupWire[] = [];
  const at = (index: number) => now - (rows - index) * STEP_MS;
  const add = (extra: Partial<WorkbenchMessage>) => {
    const index = history.length;
    const row = base(`msg_lab_${String(index + 1).padStart(4, '0')}`, new Date(at(index)).toISOString(), extra);
    history.push(row);
    return row;
  };
  for (const turn of turns) {
    if (turn.kind === 'ask') add(user(turn.text));
    else if (turn.kind === 'harness') add(harness(turn.text));
    else if (turn.kind === 'group') turn.texts.forEach((text) => add(harness(text)));
    else if (turn.kind === 'cut') {
      const ask = add(user('找一下上周的发布记录，看看有没有类似的问题。'));
      const when = Date.parse(ask.created_at) + 30_000;
      const rowsCut = cutRows(when);
      groups.push({ id: `grp_${ask.id}`, anchor_message_id: ask.id, anchor_position: 'after', open: false, status: 'interrupted', steps: rowsCut.length, duration_ms: 12_000, started_at: rowsCut[0].created_at, rows: rowsCut });
      add(user(turn.text));
    } else {
      const reply = add(agent(turn.text));
      if (turn.activity) {
        const when = Date.parse(reply.created_at);
        const records = turn.activity === 'records' ? recordRows(when, history.length) : [];
        groups.push({
          id: `grp_${reply.id}`,
          anchor_message_id: reply.id,
          anchor_position: 'before',
          open: false,
          status: 'done',
          steps: turn.activity === 'records' ? records.length : 4,
          duration_ms: turn.activity === 'records' ? 48_000 : 42_000,
          started_at: new Date(when - 50_000).toISOString(),
          rows: records,
        });
      }
    }
  }
  const runningAsk = add(user('展开执行过程的时候，能不能更像「展开」一点？做几个方案给我挑。'));
  return { history, groups, runningAsk };
}

export const labUser = user;
export const labAgent = agent;
