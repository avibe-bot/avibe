// Harness definitions and runs as `/api/harness/*` serves them, shared by the Harness page and the
// run graph's trigger panel.
import type {
  HarnessDefinitionCounts,
  HarnessRun,
  HarnessRunCounts,
  HarnessSessionSummary,
  HarnessTask,
  HarnessWatch,
} from '@/context/ApiContext';

import { DESIGN_NOW } from '../../shell/clock';
import { agentCatalog } from '../agents/fixtures';

const at = (minutesAgo: number) => new Date(DESIGN_NOW - minutesAgo * 60_000).toISOString();
const inMinutes = (minutes: number) => new Date(DESIGN_NOW + minutes * 60_000).toISOString();

const workbenchSession = (title: string): HarnessSessionSummary => ({
  session_title: title,
  session_platform: 'avibe',
  session_scope_kind: 'project',
  session_label: `avibe · ${title}`,
  session_is_workbench: true,
  session_openable: true,
});

const healthy = {
  lifecycle_detail: null,
  waiting_since: null,
  running_since: null,
  health: 'healthy' as const,
  consecutive_failures: 0,
  recent_failures: 0,
};

const task = (id: string, name: string, cron: string, extra: Partial<HarnessTask> = {}): HarnessTask => ({
  ...workbenchSession(name),
  ...healthy,
  lifecycle_state: 'waiting',
  next_run_at: inMinutes(60 * 17),
  id,
  name,
  agent_name: 'vibey',
  session_policy: 'reuse',
  session_id: `ses_${id}`,
  session_key: `avibe::project::proj_avibe::${id}`,
  prompt: '',
  message: '',
  message_payload: null,
  schedule_type: 'cron',
  cron,
  run_at: null,
  timezone: 'Asia/Shanghai',
  post_to: null,
  deliver_key: null,
  enabled: true,
  created_at: at(60 * 24 * 20),
  updated_at: at(60 * 24 * 3),
  last_run_at: at(60 * 7),
  last_run_id: `run_${id}_last`,
  last_error: null,
  ...extra,
});

export const tasks: HarnessTask[] = [
  task('tsk_ops_report', 'Slack #ops 每天 09:30 战报', '0 30 9 * * *', {
    message: '总结昨天 #ops 频道的活动，按事故、发布、待办三类发到 #ops。',
    post_to: 'slack:#ops',
  }),
  task('tsk_twitter', 'Twitter 双时段灵感', '17 10,21 * * *', {
    agent_name: 'docs-writer',
    message: '从本周的 PR 和 Release Notes 里挑两个值得发推的点，各写一版草稿。',
    next_run_at: inMinutes(60 * 2 + 17),
    last_run_at: at(60 * 11),
  }),
  task('tsk_nightly', '回归测试夜间跑', '0 0 2 * * *', {
    agent_name: 'codex',
    message: '在本地 Incus 回归环境跑完整回归，失败时贴出失败用例和日志位置。',
    health: 'failing',
    consecutive_failures: 2,
    recent_failures: 2,
    last_error: 'exit 1: 3 scenarios failed (slack_thread_reply, discord_attachment, wechat_qr)',
    last_run_at: at(60 * 6),
  }),
  task('tsk_weekly_audit', '每周依赖安全审计', '0 0 10 * * 1', {
    agent_name: 'main',
    message: '检查 avibe、avault 的依赖更新与已知漏洞，周一早上汇报。',
    enabled: false,
    lifecycle_state: 'paused',
    next_run_at: null,
    last_run_at: at(60 * 24 * 9),
  }),
];

const watch = (id: string, name: string, extra: Partial<HarnessWatch> = {}): HarnessWatch => ({
  ...workbenchSession(name),
  ...healthy,
  lifecycle_state: 'waiting',
  next_run_at: null,
  waiting_since: at(4 * 60 + 32),
  id,
  name,
  agent_name: 'vibey',
  session_policy: 'reuse',
  session_id: `ses_${id}`,
  session_key: `avibe::project::proj_avibe::${id}`,
  command: ['uv', 'run', 'wait_pr.py', '--repo', 'avibe-bot/avibe', '--pr', '3141'],
  shell_command: null,
  prefix: null,
  message: 'PR 有新的评审或 CI 结果，按交付流程处理。',
  message_payload: null,
  cwd: '/Users/demo/avibe',
  mode: 'forever',
  timeout_seconds: 0,
  lifetime_timeout_seconds: 0,
  retry_exit_codes: [75],
  retry_delay_seconds: 60,
  post_to: null,
  deliver_key: null,
  enabled: true,
  created_at: at(60 * 5),
  updated_at: at(60 * 5),
  last_started_at: at(4 * 60 + 32),
  last_finished_at: null,
  retired_at: null,
  last_event_at: null,
  last_error: null,
  last_exit_code: null,
  metadata: null,
  runtime: { running: true, pid: 51842, started_at: at(4 * 60 + 32), updated_at: at(1) },
  process_alive: true,
  ...extra,
});

export const watches: HarnessWatch[] = [
  watch('wch_pr3141', 'PR #3141 等 Codex 评审与 CI'),
  watch('wch_release', '等 v3.2.5 发布流水线', {
    agent_name: 'codex',
    command: ['gh', 'run', 'watch', '--repo', 'avibe-bot/avibe'],
    waiting_since: at(38),
    last_started_at: at(38),
  }),
];

const run = (id: string, status: HarnessRun['status'], minutesAgo: number, extra: Partial<HarnessRun> = {}): HarnessRun => ({
  ...workbenchSession('Slack #ops 每天 09:30 战报'),
  id,
  request_type: 'scheduled',
  run_type: 'scheduled',
  status,
  definition_id: 'tsk_ops_report',
  definition_name: 'Slack #ops 每天 09:30 战报',
  definition_kind: 'task',
  definition_deleted: false,
  callback_session: null,
  task_id: 'tsk_ops_report',
  source_kind: 'scheduled',
  source_actor: null,
  source_session_id: null,
  source_session: null,
  parent_run_id: null,
  callback_session_id: null,
  callback_run_id: null,
  callback_status: null,
  callback_error: null,
  agent_name: 'vibey',
  agent_id: 'agt_000001',
  agent_backend: 'vibey',
  model: 'claude-opus-5-5',
  reasoning_effort: 'medium',
  session_policy: 'reuse',
  session_key: 'avibe::project::proj_avibe::tsk_ops_report',
  session_id: 'ses_tsk_ops_report',
  post_to: 'slack:#ops',
  deliver_key: null,
  prompt: '总结昨天 #ops 频道的活动，按事故、发布、待办三类发到 #ops。',
  message: null,
  message_payload: null,
  result_text: status === 'succeeded' ? '已发到 #ops：事故 0 起，发布 2 次（v3.2.4、桌面 1.8.2），待办 3 项。' : null,
  result_payload: null,
  message_ids: [],
  cancel_requested: false,
  cancel_requested_at: null,
  pid: null,
  exit_code: status === 'succeeded' ? 0 : null,
  error: null,
  stdout: null,
  stderr: null,
  created_at: at(minutesAgo),
  started_at: status === 'queued' ? null : at(minutesAgo),
  completed_at: status === 'running' || status === 'queued' ? null : at(minutesAgo - 1),
  updated_at: at(Math.max(minutesAgo - 1, 0)),
  metadata: {},
  ok: status === 'succeeded' ? true : status === 'failed' ? false : null,
  ...extra,
});

export const runs: HarnessRun[] = [
  run('run_x8m2q4p7', 'succeeded', 7 * 60 + 30),
  run('run_9f2kc1', 'running', 26, {
    ...workbenchSession('avault 沙箱 · 后端 lane'),
    request_type: 'agent',
    run_type: 'agent',
    definition_id: null,
    definition_name: null,
    definition_kind: null,
    task_id: null,
    source_kind: 'agent',
    source_actor: 'ses_pm_vaults',
    source_session_id: 'ses_pm_vaults',
    source_session: workbenchSession('Vaults M2 · 总控分派'),
    callback_session_id: 'ses_pm_vaults',
    callback_status: 'pending',
    agent_name: 'codex',
    agent_backend: 'codex',
    model: 'gpt-5.6-sol',
    reasoning_effort: 'xhigh',
    prompt: '在 avault 沙箱里实现 M2 的后端 lane，完成后回报总控会话。',
    post_to: null,
  }),
  run('run_n1ght02', 'failed', 6 * 60, {
    ...workbenchSession('回归测试夜间跑'),
    definition_id: 'tsk_nightly',
    definition_name: '回归测试夜间跑',
    task_id: 'tsk_nightly',
    agent_name: 'codex',
    agent_backend: 'codex',
    model: 'gpt-5.6-sol',
    exit_code: 1,
    error: '3 scenarios failed (slack_thread_reply, discord_attachment, wechat_qr)',
    prompt: '在本地 Incus 回归环境跑完整回归，失败时贴出失败用例和日志位置。',
    post_to: null,
  }),
  run('run_tw1017', 'succeeded', 11 * 60, {
    ...workbenchSession('Twitter 双时段灵感'),
    definition_id: 'tsk_twitter',
    definition_name: 'Twitter 双时段灵感',
    task_id: 'tsk_twitter',
    agent_name: 'docs-writer',
    agent_backend: 'claude',
    model: 'claude-sonnet-5-5',
    prompt: '从本周的 PR 和 Release Notes 里挑两个值得发推的点，各写一版草稿。',
    result_text: '两版草稿已存进会话：「本地优先的 Agent OS」和「Vibey 的托管工具」。',
    post_to: null,
  }),
];

const definitionCounts = (rows: { lifecycle_state: string | null }[]): HarnessDefinitionCounts => ({
  total: rows.length,
  running: rows.filter((row) => row.lifecycle_state === 'running').length,
  waiting: rows.filter((row) => row.lifecycle_state === 'waiting').length,
  paused: rows.filter((row) => row.lifecycle_state === 'paused').length,
  finished: rows.filter((row) => row.lifecycle_state === 'finished').length,
});

const runCounts = (rows: HarnessRun[]): HarnessRunCounts => ({
  all: rows.length,
  queued: rows.filter((row) => row.status === 'queued').length,
  running: rows.filter((row) => row.status === 'running').length,
  succeeded: rows.filter((row) => row.status === 'succeeded').length,
  failed: rows.filter((row) => row.status === 'failed').length,
  canceled: rows.filter((row) => row.status === 'canceled').length,
});

const page = { page: 1, limit: 50, has_more: false };

/** Everything the Harness page reads for one tab, from the given rows. */
export function harnessApi(tab: 'tasks' | 'watches' | 'runs', rows = { tasks, watches, runs }): Record<string, unknown> {
  const counts = { tasks: definitionCounts(rows.tasks), watches: definitionCounts(rows.watches), runs: runCounts(rows.runs) };
  // Definition tabs open on their Active filter (running and waiting), as the server filters them.
  const active = <T extends { lifecycle_state: string | null }>(list: T[]) =>
    list.filter((row) => row.lifecycle_state === 'running' || row.lifecycle_state === 'waiting');
  const pages = {
    tasks: { ...page, counts: counts.tasks, total: active(rows.tasks).length, tasks: active(rows.tasks) },
    watches: { ...page, counts: counts.watches, total: active(rows.watches).length, watches: active(rows.watches) },
    runs: { ...page, counts: counts.runs, total: rows.runs.length, runs: rows.runs, run_types: ['scheduled', 'watch', 'agent'] },
  };
  // The page reads the default tab before the `?tab=` it was opened on, and the stub answers by path, so
  // the bootstrap page carries every list; only the asked-for tab's counts matter on screen.
  return {
    '/api/harness/bootstrap': { counts, tab, page: { ...pages.tasks, ...pages.watches, ...pages.runs, ...pages[tab] } },
    '/api/harness/counts': counts,
    // Unfiltered lists, as the run graph's trigger panel reads them.
    '/api/harness/tasks': { ...pages.tasks, total: rows.tasks.length, tasks: rows.tasks },
    '/api/harness/watches': { ...pages.watches, total: rows.watches.length, watches: rows.watches },
    '/api/harness/runs': pages.runs,
    ...Object.fromEntries(
      [...rows.tasks, ...rows.watches].map(({ id }) => {
        const own = rows.runs.filter((row) => row.definition_id === id);
        return [`/api/harness/runs?definition_id=${id}&limit=6`, { ...page, counts: runCounts(own), total: own.length, runs: own }];
      }),
    ),
    ...Object.fromEntries(rows.runs.map((row) => [`/api/harness/runs/${row.id}`, { ok: true, run: row }])),
    '/api/workbench/prefs': { background_work_banner_enabled: true },
    '/api/agents': agentCatalog,
  };
}
