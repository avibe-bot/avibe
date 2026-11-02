// The execution process (执行过程) in chat, rendered by the shipped ChatPage with `ui.show_agent_activity`
// on: while a turn runs, a compact live card at the transcript's tail (and expanded, on a click); once it
// settles, a one-line chip just above the reply (done), below the message that started it (interrupted), or
// above the failure notice (failed), which opens into the steps it took.
import { frames } from '../../frame';
import {
  activityGroup,
  agentSays,
  chatDesktop,
  chatFixtures,
  chatPhone,
  chatRoute,
  failureApi,
  failureNotice,
  narration,
  runningTurn,
  toolCall,
  userSays,
} from './fixtures';

const claude = { title: '研报对比 · 宁德时代', agent_name: 'claude', agent_backend: 'claude', agent_variant: 'claude', model: 'claude-opus-5-5' };
const ui = { show_agent_activity: true };

const s = (seconds: number) => seconds / 60;

// ----- A turn in flight -----

const ask = userSays('对比中金和高盛对宁德时代 2026 年盈利预测的差异，列个表并标出关键假设。', s(52));
const liveRows = [
  narration('先读两份研报的盈利预测章节。', s(47)),
  toolCall('Read', { file_path: '/Users/demo/research/中金_宁德时代_2026展望.pdf' }, s(45)),
  toolCall('Read', { file_path: '/Users/demo/research/高盛_CATL_2026Q1.pdf' }, s(43)),
  toolCall('Bash', { command: 'pdftotext -layout 中金_宁德时代_2026展望.pdf - | rg -n "盈利预测" -A 12', description: '提取两份研报的盈利预测表' }, s(40)),
  narration('两份研报的营收口径不同：中金含储能，高盛只算动力电池。先统一到「含储能」口径，再比 2026E 归母净利。', s(30)),
  toolCall('WebSearch', { query: 'CATL 2026 consensus net profit forecast' }, s(12)),
];
const running = chatFixtures({
  session: claude,
  ui,
  messages: [ask],
  turnState: runningTurn,
  activity: [activityGroup('grp_live', ask, liveRows, { status: 'running' })],
});

// ----- Settled turns: done, interrupted, failed -----

const ask1 = userSays('对比中金和高盛对宁德时代 2026 年盈利预测的差异，列个表并标出关键假设。', 40);
const doneRows = [
  narration('先读两份研报的盈利预测章节。', 39.5),
  toolCall('Read', { file_path: '/Users/demo/research/中金_宁德时代_2026展望.pdf' }, 39.4),
  toolCall('Read', { file_path: '/Users/demo/research/高盛_CATL_2026Q1.pdf' }, 39.3),
  toolCall('Bash', { command: 'pdftotext -layout 中金_宁德时代_2026展望.pdf - | rg -n "盈利预测" -A 12', description: '提取两份研报的盈利预测表' }, 39.2),
  narration('口径不一致：中金营收含储能，高盛只算动力电池。先统一到「含储能」口径。', 39),
  toolCall('WebSearch', { query: 'CATL 2026 consensus net profit forecast' }, 38.9),
  toolCall('Bash', { command: 'python3 normalize.py --basis energy-storage', description: '统一口径重算差异' }, 38.8),
  toolCall('Write', { file_path: '/Users/demo/research/CATL_2026_对比.md', content: '…' }, 38.7),
];
const reply1 = agentSays(
  '对比好了：2026E 归母净利中金 **612 亿** vs 高盛 **545 亿**，差 12%，主要来自储能出货假设。\n\n| | 中金 | 高盛 |\n| --- | --- | --- |\n| 2026E 营收 | 5,840 亿 | 5,210 亿 |\n| 2026E 归母净利 | 612 亿 | 545 亿 |\n| 储能出货 | 180 GWh | 120 GWh |\n\n表格也存在 `CATL_2026_对比.md` 里了。',
  38.6,
);
const ask2 = userSays('再加上瑞银的预测。', 30);
const interruptedRows = [
  narration('找瑞银的最新研报。', 29.8),
  toolCall('Glob', { pattern: '/Users/demo/research/*瑞银*' }, 29.7),
  toolCall('WebSearch', { query: 'UBS CATL 2026 forecast' }, 29.6),
];
const ask3 = userSays('继续。', 12);
const failedRows = [
  narration('接着上次，读瑞银研报。', 11.9),
  toolCall('Read', { file_path: '/Users/demo/research/瑞银_CATL_2026.pdf' }, 11.8),
  toolCall('Bash', { command: 'pdftotext -layout 瑞银_CATL_2026.pdf -', description: '提取瑞银研报正文' }, 11.7),
];
const notice = failureNotice('turn_ubs', 11.5);

const settledFixture = (extra: { ui?: Record<string, unknown> } = {}) =>
  chatFixtures({
    session: claude,
    ui: { ...ui, ...extra.ui },
    messages: [ask1, reply1, ask2, ask3, notice],
    activity: [
      activityGroup('grp_done', reply1, doneRows, { duration_ms: 83_000 }),
      activityGroup('grp_cut', ask2, interruptedRows, { status: 'interrupted' }),
      activityGroup('grp_failed', notice, failedRows, { status: 'failed' }),
    ],
    api: failureApi('turn_ubs'),
  });

const openChip = 'button[title="{t:chat.agentActivity.expand}"] >> nth=0';

const designFrames = frames([
  {
    name: 'running',
    title: '执行过程 · 运行中（紧凑）',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(),
    fixtures: running,
    render: chatDesktop,
  },
  {
    name: 'running-expanded',
    title: '执行过程 · 运行中 · 展开全部',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: chatRoute(),
    fixtures: running,
    click: ['role=button[name="{t:chat.agentActivity.collapse}"]'],
    render: chatDesktop,
  },
  {
    name: 'settled',
    title: '执行过程 · 完成、中断与失败后收起为一行',
    width: 1192,
    height: 960,
    status: 'shipped',
    route: chatRoute(),
    fixtures: settledFixture(),
    render: chatDesktop,
  },
  {
    name: 'settled-expanded',
    title: '执行过程 · 点开已完成的一轮',
    width: 1192,
    height: 1180,
    status: 'shipped',
    route: chatRoute(),
    fixtures: settledFixture(),
    click: [openChip],
    render: chatDesktop,
  },
  {
    name: 'tool-detail',
    title: '执行过程 · 展开一次工具调用的参数',
    width: 1192,
    height: 1320,
    status: 'shipped',
    route: chatRoute(),
    fixtures: settledFixture(),
    click: [openChip, 'button[title="{t:chat.agentActivity.showCall}"] >> nth=2'],
    render: chatDesktop,
  },
  {
    name: 'tool-json',
    title: '执行过程 · 工具调用的完整参数（JSON）',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: chatRoute(),
    fixtures: settledFixture(),
    click: [
      openChip,
      'button[title="{t:chat.agentActivity.showCall}"] >> nth=2',
      'role=button[name="{t:chat.agentActivity.viewJson}"]',
    ],
    render: chatDesktop,
  },
  {
    name: 'tools-hidden',
    title: '执行过程 · 隐藏工具调用，只看叙述',
    width: 1192,
    height: 1000,
    status: 'shipped',
    route: chatRoute(),
    fixtures: settledFixture({ ui: { show_tool_calls: false } }),
    click: [openChip],
    render: chatDesktop,
  },
  {
    name: 'phone-running',
    title: '执行过程 · 运行中 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: running,
    render: chatPhone,
  },
  {
    name: 'phone-settled-expanded',
    title: '执行过程 · 点开已完成的一轮 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({
      session: claude,
      ui,
      messages: [ask1, reply1],
      activity: [activityGroup('grp_done', reply1, doneRows, { duration_ms: 83_000 })],
    }),
    click: [openChip],
    render: chatPhone,
  },
]);

export default designFrames;
