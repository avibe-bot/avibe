// Vibe Agents as `GET /api/agents` and the settings' agent catalog serve them, shared by the
// Agents page and every surface that picks an Agent (routing panels, the new session sheet).
import type { VibeAgentBrief } from '@/context/ApiContext';
import type { AgentGraphEdge, AgentGraphNode, AgentGraphResult, AgentGraphTriggerNode } from '@/lib/agentGraph';

import { DESIGN_NOW } from '../../shell/clock';

const at = (minutesAgo: number) => new Date(DESIGN_NOW - minutesAgo * 60_000).toISOString();

let nextId = 0;
export const agentBrief = (name: string, backend: string, extra: Partial<VibeAgentBrief> = {}): VibeAgentBrief => ({
  id: `agt_${(nextId += 1).toString(16).padStart(6, '0')}`,
  name,
  display_name: name,
  description: null,
  backend,
  model: null,
  reasoning_effort: null,
  enabled: true,
  archived: false,
  archived_at: null,
  source: 'user',
  updated_at: at(60 * 24),
  bot: null,
  ...extra,
});

/** The built-in Agents a fresh install has, plus the user's own. */
export const demoAgents: VibeAgentBrief[] = [
  agentBrief('vibey', 'vibey', { source: 'builtin', description: 'Vibey, the official agent of Avibe.' }),
  agentBrief('main', 'vibey', { source: 'builtin', description: "The user's main Bot and first point of contact.", bot: { handle: 'main', display_name: '', builtin: true } }),
  agentBrief('claude', 'claude', { source: 'builtin', model: 'claude-opus-5-5', description: 'Default Agent for the Claude Code backend.' }),
  agentBrief('codex', 'codex', { source: 'builtin', model: 'gpt-5.6-sol', reasoning_effort: 'high', description: 'Default Agent for the Codex backend.' }),
  agentBrief('opencode', 'opencode', { source: 'builtin', model: 'openai/gpt-5.6-sol', description: 'Default Agent for the OpenCode backend.' }),
  agentBrief('reviewer', 'claude', { model: 'claude-opus-5-5', reasoning_effort: 'high', description: '代码评审：只看 diff，先列缺陷再给建议' }),
  agentBrief('docs-writer', 'claude', { model: 'claude-sonnet-5-5', description: '中英文档写作与校对' }),
  agentBrief('slack-triage', 'opencode', { model: 'openai/gpt-5.6-sol', reasoning_effort: 'low', enabled: false, description: '把 Slack 消息归类、找负责人、转成 PR' }),
];

/** `{ agents, default_agent_name }`, as `/api/agents` and `/api/settings` (`agent_catalog`) answer. */
export const agentCatalog = { ok: true, agents: demoAgents, default_agent_name: 'vibey' };

const SYSTEM_PROMPT = `你是团队的代码评审。只看本次 diff：
1. 先列出会导致错误行为的缺陷，按严重程度排序；
2. 每条给出文件和行号，以及最小修复建议；
3. 没有缺陷时直接说“没有发现问题”，不要凑数。`;

const efforts = (...values: string[]) => values.map((value) => ({ value, label: value }));

/** Each backend's model menu as a Direct-mode install reads it: Model Hub answers Direct, the CLI lists its models. */
export const modelMenusApi: Record<string, unknown> = {
  ...Object.fromEntries(
    ['vibey', 'claude', 'codex', 'opencode'].map((backend) => [`/api/models/agents/${backend}/models`, { ok: true, agent: { backend, mode: 'direct' } }]),
  ),
  '/api/claude/models': {
    ok: true,
    models: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5'],
    model_labels: { 'claude-opus-5-5': 'Opus 5.5', 'claude-sonnet-5-5': 'Sonnet 5.5', 'claude-haiku-5': 'Haiku 5' },
    reasoning_options: {
      'claude-opus-5-5': efforts('low', 'medium', 'high', 'max'),
      'claude-sonnet-5-5': efforts('low', 'medium', 'high'),
      'claude-haiku-5': [],
    },
  },
  '/api/codex/models': {
    ok: true,
    models: ['gpt-5.6-sol', 'gpt-5.6-sol-mini'],
    reasoning_options: { 'gpt-5.6-sol': efforts('low', 'medium', 'high', 'xhigh'), 'gpt-5.6-sol-mini': efforts('low', 'medium', 'high') },
  },
};

/** The Agents page's reads: the list, the selected Agent in full, the gateway supply, and running work. */
export function agentsApi(selected?: string): Record<string, unknown> {
  const api: Record<string, unknown> = {
    '/api/agents': agentCatalog,
    '/api/models/agents': { agents: [] },
    '/api/agent-onboarding': { ok: true, available: false, organization_id: null, agents: [], counts: { total: 0, system: 0, custom: 0, not_onboarded: 0, private: 0, published: 0, conflicts: 0 } },
    '/api/running-agents': { ok: true, agents: [], counts: { total: 0, active: 0, idle: 0, orphan: 0, by_backend: {} } },
    ...modelMenusApi,
  };
  for (const agent of demoAgents) {
    api[`/api/agents/${agent.name}`] = {
      ok: true,
      default_agent_name: agentCatalog.default_agent_name,
      agent: {
        ...agent,
        system_prompt: agent.name === selected ? SYSTEM_PROMPT : null,
        metadata: {},
        created_at: agent.updated_at,
      },
    };
  }
  return api;
}

// ── Run graph ────────────────────────────────────────────────────────────────

const graphNode = (
  sessionId: string,
  title: string,
  agent: string,
  backend: string,
  status: AgentGraphNode['status'],
  minutesAgo: number,
  extra: Partial<AgentGraphNode> = {},
): AgentGraphNode => {
  const live = status === 'active' || status === 'idle' || status === 'orphan';
  return {
    session_id: sessionId,
    title,
    agent_name: agent,
    agent_backend: backend,
    model: backend === 'codex' ? 'gpt-5.6-sol' : 'claude-opus-5-5',
    reasoning_effort: backend === 'codex' ? 'xhigh' : 'high',
    status,
    live,
    visibility: 'background',
    scope_id: 'avibe::project::proj_avibe',
    project_id: 'proj_avibe',
    scope_label: 'avibe',
    platform: 'avibe',
    workdir: '/Users/demo/avibe/.worktrees/avault/sandbox',
    openable_in_chat: true,
    created_at: at(minutesAgo + 30),
    last_active_at: at(minutesAgo),
    elapsed_seconds: status === 'active' ? minutesAgo * 60 : null,
    run_counts: { total: 3, running: status === 'active' ? 1 : 0 },
    runs: [
      { id: `run_${sessionId}_3`, status: status === 'active' ? 'running' : status === 'failed' ? 'failed' : 'succeeded', run_type: 'agent', created_at: at(minutesAgo), started_at: at(minutesAgo), completed_at: status === 'active' ? null : at(minutesAgo - 1) },
      { id: `run_${sessionId}_2`, status: 'succeeded', run_type: 'agent', created_at: at(minutesAgo + 12), started_at: at(minutesAgo + 12), completed_at: at(minutesAgo + 8) },
      { id: `run_${sessionId}_1`, status: 'succeeded', run_type: 'agent', created_at: at(minutesAgo + 30), started_at: at(minutesAgo + 30), completed_at: at(minutesAgo + 18) },
    ],
    ...extra,
  };
};

export const graphNodes: AgentGraphNode[] = [
  graphNode('ses_pm_vaults', 'Vaults M2 · 总控分派', 'claude', 'claude', 'active', 72, { visibility: 'foreground', workdir: '/Users/demo/avibe' }),
  graphNode('ses_avault_lane', 'avault 沙箱 · 后端 lane', 'codex', 'codex', 'active', 26),
  graphNode('ses_unlock_ux', '解锁 UX · 前端 lane', 'claude', 'claude', 'active', 32),
  graphNode('ses_protocol', '协议 v2 · 契约冻结', 'codex', 'codex', 'succeeded', 8),
  graphNode('ses_pr38', 'PR #38 · review 跟进', 'reviewer', 'claude', 'idle', 4),
  graphNode('ses_twitter', 'Twitter 选题草稿', 'docs-writer', 'claude', 'succeeded', 60 * 5 + 36, { scope_label: 'writing', project_id: null, scope_id: null }),
  graphNode('ses_pr934', 'pr934 · review 修复', 'codex', 'codex', 'queued', 1),
  graphNode('ses_mobile_chat', '移动端适配 · Chat', 'claude', 'claude', 'succeeded', 60 * 20, { visibility: 'foreground' }),
  graphNode('ses_ios_keyboard', 'iOS 键盘修复', 'claude', 'claude', 'succeeded', 60 * 19),
  graphNode('ses_scroll_lock', '滚动锁 · 回归验证', 'codex', 'codex', 'failed', 60 * 18),
];

export const graphTriggers: AgentGraphTriggerNode[] = [
  { definition_id: 'tsk_twitter', definition_type: 'scheduled', name: 'Twitter 双时段灵感', schedule_label: 'cron 10:17', enabled: true },
  { definition_id: 'wch_pr3141', definition_type: 'watch', name: 'PR #3141 等 Codex 评审与 CI', schedule_label: 'PR 评审', enabled: true },
];

const spawn = (from: string, to: string): AgentGraphEdge => ({ kind: 'spawn', from, to, run_count: 1, last_at: at(30) });

export const graphEdges: AgentGraphEdge[] = [
  spawn('ses_pm_vaults', 'ses_avault_lane'),
  spawn('ses_pm_vaults', 'ses_unlock_ux'),
  spawn('ses_pm_vaults', 'ses_protocol'),
  spawn('ses_avault_lane', 'ses_pr38'),
  { kind: 'callback', from: 'ses_protocol', to: 'ses_pm_vaults', status: 'sent', last_at: at(8) },
  { kind: 'callback', from: 'ses_avault_lane', to: 'ses_pm_vaults', status: 'pending', last_at: at(26) },
  { kind: 'trigger', from: 'def:tsk_twitter', to: 'ses_twitter', run_count: 1, last_at: at(60 * 5 + 36) },
  { kind: 'trigger', from: 'def:wch_pr3141', to: 'ses_pr934', run_count: 1, last_at: at(1) },
  spawn('ses_mobile_chat', 'ses_ios_keyboard'),
  spawn('ses_mobile_chat', 'ses_scroll_lock'),
];

export const agentGraph: AgentGraphResult = {
  ok: true,
  generated_at: at(0),
  window: '24h',
  counts: { active: 3, idle: 1, queued: 1, ended: 5, background: 8, foreground: 2 },
  nodes: graphNodes,
  trigger_nodes: graphTriggers,
  edges: graphEdges,
  truncated: false,
};

/** The run graph's reads, plus the running work its badge counts. */
export const graphApi: Record<string, unknown> = {
  '/api/agents-graph': agentGraph,
  '/api/running-agents': {
    ok: true,
    agents: [],
    counts: { total: 3, active: 3, idle: 0, orphan: 0, by_backend: { claude: 2, codex: 1 } },
  },
};
