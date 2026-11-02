// Vibe Agents as `GET /api/agents` and the settings' agent catalog serve them, shared by the
// Agents page and every surface that picks an Agent (routing panels, the new session sheet).
import type { VibeAgentBrief } from '@/context/ApiContext';

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
  agentBrief('slack-triage', 'opencode', { model: 'openai/gpt-5.6-sol', reasoning_effort: 'low', description: '把 Slack 消息归类、找负责人、转成 PR' }),
];

/** `{ agents, default_agent_name }`, as `/api/agents` and `/api/settings` (`agent_catalog`) answer. */
export const agentCatalog = { ok: true, agents: demoAgents, default_agent_name: 'vibey' };
