// Fixture builders for the Settings · Backends frames: each backend's runtime, connection, and
// sign-in reads, on top of the Settings pages' config and the Model Hub's agent supply.
import type { BackendConnectionState, BackendRuntimeInfo, ClaudeAuthState, CodexAuthState, OpencodeProvider } from '@/context/ApiContext';

import { settingsConfig, settingsFixtures } from '../settings/fixtures';
import { chatgptPro, claudeMax, deepseek, hubApi, relayClaude, relayGpt, type AgentSpec } from './fixtures';

type Backend = 'claude' | 'codex' | 'opencode';

const runtime = (name: Backend, extra: Partial<BackendRuntimeInfo> = {}): BackendRuntimeInfo => ({
  ok: true,
  name,
  enabled: true,
  auto_update: true,
  cli_path: name,
  resolved_path: `/Users/demo/.local/bin/${name}`,
  installed: true,
  status: 'ok',
  reason: null,
  current_version: '2.1.32',
  latest_version: '2.1.32',
  has_update: false,
  supports_restart: true,
  process_status: 'running',
  ...extra,
});

/** Claude Code has an update waiting, Codex is current, OpenCode's CLI is missing from PATH. */
export const RUNTIMES: Record<Backend, BackendRuntimeInfo> = {
  claude: runtime('claude', { current_version: '2.1.30', latest_version: '2.1.32', has_update: true }),
  codex: runtime('codex', { current_version: '0.66.0', latest_version: '0.66.0' }),
  opencode: runtime('opencode', { current_version: '1.4.11', latest_version: '1.4.11' }),
};

const connection = (backend: Backend, extra: Partial<BackendConnectionState> = {}): BackendConnectionState => ({
  ok: true,
  backend,
  installed: true,
  cli_status: 'ok',
  enabled: true,
  auth: 'subscription',
  application: 'applied',
  ready: true,
  entry_eligible: true,
  supply_mode: 'direct',
  ...extra,
});

const claudeAuth: ClaudeAuthState = {
  ok: true,
  auth_mode: 'oauth',
  active_auth_mode: 'oauth',
  oauth_store_state: 'present',
  can_remove_oauth_credentials: true,
  has_api_key: false,
  api_key_length: 0,
  api_key_masked: null,
  api_key_source: null,
  has_oauth_credentials: true,
  base_url: null,
  settings_path: '/Users/demo/.claude/settings.json',
  settings_exists: true,
  settings_env_has_key: false,
  settings_env_key_length: 0,
  settings_env_key_var: null,
  credential_type: null,
  settings_env_base_url: null,
  settings_conflict: false,
};

const codexAuth: CodexAuthState = {
  ok: true,
  auth_mode: 'api_key',
  active_auth_mode: 'api_key',
  has_api_key: true,
  api_key_length: 164,
  api_key_masked: 'sk-proj…7Fq2',
  base_url: 'https://ai-relay.chainbot.io/v1',
  has_chatgpt_tokens: false,
  chatgpt_account: null,
  credentials_store: 'file',
  file_store_active: true,
};

const provider = (id: string, name: string, models: string[], extra: Partial<OpencodeProvider> = {}): OpencodeProvider => ({
  id, name, description: '', configured: false, oauth_available: false, local: false, models, ...extra,
});

const opencodeProviders = {
  ok: true,
  default_provider: 'deepseek',
  permission_allowed: true,
  providers: [
    provider('deepseek', 'DeepSeek', ['deepseek-v4', 'deepseek-v4-reasoner'], { configured: true, has_auth: true, api_key_masked: 'sk-4f1…a09c', active_auth_type: 'api' }),
    provider('anthropic', 'Anthropic', ['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5'], { configured: true, has_auth: true, oauth_available: true, active_auth_type: 'oauth' }),
    provider('openai', 'OpenAI', ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.5'], { oauth_available: true }),
    provider('moonshotai', 'Moonshot AI', ['kimi-k3', 'kimi-k3-thinking']),
    provider('zhipuai', 'Zhipu AI', ['glm-5.2', 'glm-5.2-air']),
    provider('ollama', 'Ollama', ['qwen3:32b', 'llama4:scout'], { local: true }),
  ],
};

const modelCatalog = (models: string[]) => ({
  ok: true,
  models,
  reasoning_options: Object.fromEntries(models.map((model) => [model, ['low', 'medium', 'high'].map((value) => ({ value, label: value }))])),
});

/** Claude Code signs in natively; Codex and OpenCode take their models from the gateway. */
const agents: AgentSpec[] = [
  { backend: 'claude', mode: 'direct' },
  { backend: 'codex', order: [chatgptPro, relayGpt], routes: { 'gpt-6-astra': { hops: [[chatgptPro, 'gpt-6-astra'], [relayGpt, 'gpt-6-astra']] }, 'gpt-5.6-sol': { hops: [[relayGpt, 'gpt-5.6-sol']] } } },
  { backend: 'opencode', order: [deepseek, relayClaude], routes: { 'deepseek-v4': { hops: [[deepseek, 'deepseek-v4']] }, 'claude-sonnet-5': { hops: [[relayClaude, 'claude-sonnet-5']] } } },
];

/** A different state for some backends: its runtime projection, whether it is switched on, its other reads. */
export type BackendsSpec = {
  runtimes?: Partial<Record<Backend, Partial<BackendRuntimeInfo>>>;
  enabled?: Partial<Record<Backend, boolean>>;
  api?: Record<string, unknown>;
};

/** Everything the Backends pages read: the Owner's config (every backend on unless `enabled` says
 *  otherwise), each backend's runtime, connection and sign-in, and the Model Hub's agent supply. */
export function backendsFixtures({ runtimes = {}, enabled = {}, api = {} }: BackendsSpec = {}) {
  const agentConfig = settingsConfig.agents as Record<string, Record<string, unknown>>;
  const config = {
    ...settingsConfig,
    agents: {
      ...agentConfig,
      claude: { ...agentConfig.claude, enabled: enabled.claude ?? true },
      codex: { ...agentConfig.codex, enabled: enabled.codex ?? true, auth_mode: 'api_key', has_api_key: true, api_key_length: 164 },
      opencode: { ...agentConfig.opencode, enabled: enabled.opencode ?? true },
    },
  };
  return settingsFixtures({
    ...hubApi({ sources: [claudeMax, chatgptPro, relayGpt, relayClaude, deepseek], agents }),
    '/api/config': config,
    ...Object.fromEntries((Object.keys(RUNTIMES) as Backend[]).map((name) => [
      `/api/backend/${name}/runtime`,
      { ...RUNTIMES[name], enabled: enabled[name] ?? true, ...runtimes[name] },
    ])),
    '/api/backend/claude/connection': connection('claude'),
    '/api/backend/codex/connection': connection('codex', { auth: 'api_key', supply_mode: 'hub' }),
    '/api/backend/opencode/connection': connection('opencode', { auth: 'api_key', supply_mode: 'hub' }),
    '/api/backend/claude/auth': claudeAuth,
    '/api/backend/codex/auth': codexAuth,
    '/api/backend/opencode/providers': opencodeProviders,
    '/api/claude/models': modelCatalog(['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5']),
    '/api/codex/models': modelCatalog(['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.5']),
    ...api,
  });
}
