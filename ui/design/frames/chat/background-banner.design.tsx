// The background-work banner above the composer, rendered by the shipped ChatPage: one pill for everything
// still running for this chat (background commands, Watches, scheduled tasks, delegated runs), counted,
// headed by the first; a click opens the list upward, each Harness row linking to its Harness view.
import type { SessionActivityState } from '@/context/ApiContext';

import { frames } from '../../frame';
import { CHAT_ID, agentSays, at, chatDesktop, chatFixtures, chatPhone, chatRoute, idleTurn, userSays } from './fixtures';

const activity = (id: string, extra: Partial<SessionActivityState>): SessionActivityState => ({
  id,
  backend: 'claude',
  runtime_key: `claude::${CHAT_ID}`,
  session_id: CHAT_ID,
  kind: 'background_command',
  status: 'running',
  description: null,
  started_at: at(2),
  updated_at: at(1),
  ...extra,
});

const command = activity('bg_npm_test', { description: 'npm run test -w ui', started_at: at(2), since: at(2) });
const watch = activity('watch:watch_pr3164', { item_kind: 'watch', status: 'enabled', label: 'PR #3164 评审', since: at(40) });
const task = activity('task:task_daily_digest', { item_kind: 'task', status: 'scheduled', label: '每日晨报', schedule_type: 'cron', since: at(60 * 7) });
const run = activity('agent_run:run_7f2c', { item_kind: 'agent_run', status: 'running', label: 'codex — 修复 #3162', since: at(60) });

const messages = [
  userSays('跑一下 UI 的单测，同时盯着 PR #3164 的评审。', 6),
  agentSays('单测已在后台跑，PR #3164 也加了 Watch：有评审或 CI 结果会回到这个会话。修 #3162 的活交给了 codex。', 4),
];

const claude = { title: 'PR #3164 交付', agent_name: 'claude', agent_backend: 'claude', agent_variant: 'claude', model: 'claude-opus-5-5' };
const banner = (background: SessionActivityState[]) =>
  chatFixtures({ session: claude, messages, turnState: { ...idleTurn, background_activities: background } });

const open = ['role=button[name="{t:chat.activities.toggle}"]'];

const designFrames = frames([
  {
    name: 'one',
    title: '后台工作 · 一项',
    width: 1192,
    height: 560,
    status: 'shipped',
    route: chatRoute(),
    fixtures: banner([command]),
    render: chatDesktop,
  },
  {
    name: 'many',
    title: '后台工作 · 多项聚合为一条',
    width: 1192,
    height: 560,
    status: 'shipped',
    route: chatRoute(),
    fixtures: banner([command, watch, task, run]),
    render: chatDesktop,
  },
  {
    name: 'many-open',
    title: '后台工作 · 点开明细',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(),
    fixtures: banner([command, watch, task, run]),
    click: open,
    render: chatDesktop,
  },
  {
    name: 'delivering',
    title: '后台工作 · 正在投递后台输出',
    width: 1192,
    height: 560,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: claude, messages, turnState: { ...idleTurn, pending_activity_output_count: 2 } }),
    render: chatDesktop,
  },
  {
    name: 'phone-open',
    title: '后台工作 · 点开明细 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: banner([command, watch, task, run]),
    click: open,
    render: chatPhone,
  },
]);

export default designFrames;
