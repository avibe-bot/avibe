// Fixture builders for the setup frames: the `/setup` journey a fresh install opens on (intro,
// model providers, AI assistants) and the dialogs it opens, served through the design network stub.
// The journey reads the config, each assistant's CLI and connection, the Model Hub (sources, agent
// supply, routes, the gateway engine), and each card's built-in Agent.
import type { BackendConnectionState, BackendRuntimeInfo, ClaudeAuthState, CliStatus, CodexAuthState, OpencodeProvider, VibeAgentFull } from '@/context/ApiContext';
import type { MigrationItem, RuntimeHealth, Source, SuppliedModel } from '@/components/settings/models/types';

import { agentBrief } from '../agents/fixtures';
import { at, hubApi, ok, runtime, source, type AgentSpec } from '../models/fixtures';
import { settingsConfig } from '../settings/fixtures';

/** A fresh install's `GET /api/config`: setup not finished, no chat platform connected, the
 *  model gateway available and switched on, the three CLI assistants on by default. */
export const setupConfig = {
  ...settingsConfig,
  setup_completed: false,
  platform: 'avibe',
  platforms: { enabled: [], primary: 'avibe' },
  model_hub: { enabled: true, runtime_default_applied: true, sources: [], agents: {} },
  capabilities: { model_hub: { enabled: true } },
  agent_supply_blocks: {},
  agents: {
    ...settingsConfig.agents,
    claude: { ...settingsConfig.agents.claude, enabled: true },
    codex: { ...settingsConfig.agents.codex, enabled: true },
    opencode: { ...settingsConfig.agents.opencode, enabled: true },
  },
};

// ── Model providers ─────────────────────────────────────────────────────

const supplied = (ids: string[], efforts: string[]): SuppliedModel[] =>
  ids.map((id) => ({ id, origin: 'discovered', reasoning_efforts: efforts, reasoning_efforts_source: 'catalog', discovered_at: at(-12) }));

const healthy = { status: 'active', retry_at: null, detail_key: null } as const;

/** The two providers a person adds first: an OpenAI and an Anthropic API key, both on the gateway. */
export const openaiKey: Source = source('src_openai', {
  vendor: 'openai',
  display_name: 'OpenAI',
  protocol: 'openai_responses',
  state: healthy,
  masked_credential: 'sk-proj…7Fq2',
  models: supplied(['gpt-5.6-sol', 'gpt-5.6-mini', 'gpt-5.6-nano'], ['low', 'medium', 'high', 'xhigh']),
});

export const anthropicKey: Source = source('src_anthropic', {
  vendor: 'anthropic',
  display_name: 'Anthropic',
  protocol: 'anthropic',
  state: healthy,
  masked_credential: 'sk-ant…9c2E',
  models: supplied(['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5'], ['low', 'medium', 'high', 'max']),
});

/** What Vibey could run on with no model yet: the models the added providers supply. */
const candidate = (src: Source, id: string) => ({
  id,
  display_name: null,
  reasoning_efforts: src.models.find((model) => model.id === id)?.reasoning_efforts ?? [],
  suppliers: [{ source_id: src.id, source_name: src.display_name, model_id: id }],
  origin: 'provider',
  native_protocol: src.protocol,
});

export const candidatesApi = {
  '/api/models/agents/vibey/models/candidates': ok({
    candidates: {
      builtin: [],
      providers: [
        ...openaiKey.models.map((model) => candidate(openaiKey, model.id)),
        ...anthropicKey.models.map((model) => candidate(anthropicKey, model.id)),
      ],
      in_list: [],
    },
  }),
};

// ── Assistants ──────────────────────────────────────────────────────────

type Cli = 'claude' | 'codex' | 'opencode';
type Assistant = Cli | 'vibey';

export type AssistantSpec = {
  /** The model the assistant's built-in Agent is set to; `null` when it has none. */
  model?: string | null;
  /** Switched on in the config (CLI assistants; Vibey is always on). */
  enabled?: boolean;
  /** What the CLI probe finds. */
  cli?: CliStatus;
  /** The installed CLI has a newer version. */
  update?: boolean;
  /** Who supplies its credentials: the gateway (setup's default) or its own sign-in. */
  mode?: 'hub' | 'direct';
  /** The connection a direct assistant has: its own subscription, an API key, or nothing yet. */
  auth?: BackendConnectionState['auth'];
};

/** Each assistant on the gateway, with the model its built-in Agent uses and the source serving it. */
const DEFAULT_ASSISTANTS: Record<Assistant, AssistantSpec> = {
  vibey: { model: 'gpt-5.6-sol' },
  claude: { model: 'claude-opus-5' },
  codex: { model: 'gpt-5.6-sol' },
  opencode: { model: 'gpt-5.6-sol' },
};

const VERSIONS: Record<Cli, [string, string]> = {
  claude: ['2.1.30', '2.1.32'],
  codex: ['0.66.0', '0.66.2'],
  opencode: ['1.4.11', '1.4.13'],
};

const cliRuntime = (name: Cli, spec: AssistantSpec): BackendRuntimeInfo => {
  const status = spec.cli ?? 'ok';
  const [current, latest] = VERSIONS[name];
  return {
    ok: true,
    name,
    enabled: spec.enabled ?? true,
    auto_update: true,
    cli_path: name,
    resolved_path: status === 'missing' ? null : `/Users/demo/.local/bin/${name}`,
    installed: status !== 'missing',
    status,
    reason: status === 'missing' ? 'not_found' : null,
    current_version: status === 'missing' ? null : current,
    latest_version: latest,
    has_update: status === 'ok' && !!spec.update,
    supports_restart: true,
    process_status: 'running',
  };
};

const connection = (backend: Assistant, spec: AssistantSpec): BackendConnectionState => {
  const mode = spec.mode ?? 'hub';
  const auth = spec.auth ?? (mode === 'hub' ? 'api_key' : 'none');
  return {
    ok: true,
    backend,
    installed: backend === 'vibey' || spec.cli !== 'missing',
    cli_status: backend === 'vibey' ? null : spec.cli ?? 'ok',
    enabled: backend === 'vibey' || (spec.enabled ?? true),
    auth,
    application: 'applied',
    ready: auth !== 'none',
    entry_eligible: auth !== 'none',
    supply_mode: mode,
  };
};

/** The built-in Agent each card speaks for, as an uncached full read answers it. */
const builtinAgent = (backend: Assistant, model: string | null): VibeAgentFull => ({
  ...agentBrief(backend, backend, { source: 'builtin', model, description: `Default Agent for the ${backend} backend.` }),
  system_prompt: null,
  metadata: { builtin_default: true, lock_delete: true },
  created_at: at(-60),
});

export type SetupSpec = {
  /** The model gateway is switched off in this install's config (`model_hub.enabled`). */
  gatewayOff?: boolean;
  /** The deployment disabled the model gateway on this instance (`capabilities.model_hub`). */
  hubDisabled?: boolean;
  /** The providers added so far. */
  sources?: Source[];
  /** How the gateway engine reads. */
  health?: RuntimeHealth;
  /** Per-assistant differences from every assistant ready on the gateway. */
  assistants?: Partial<Record<Assistant, AssistantSpec>>;
  /** OpenCode still needs its tool-call permission. */
  permissionRequired?: boolean;
  /** Native logins and keys the gateway could take over, as the migration scan finds them. */
  migration?: MigrationItem[];
  /** Other reads, by path. */
  api?: Record<string, unknown>;
};

/** Everything the setup journey reads, for one description of the install. */
export function setupFixtures({
  gatewayOff = false,
  hubDisabled = false,
  sources = [openaiKey, anthropicKey],
  health = 'ok',
  assistants = {},
  permissionRequired = false,
  migration = [],
  api = {},
}: SetupSpec = {}) {
  const specs = Object.fromEntries(
    (Object.keys(DEFAULT_ASSISTANTS) as Assistant[]).map((name) => [name, { ...DEFAULT_ASSISTANTS[name], ...assistants[name] }]),
  ) as Record<Assistant, AssistantSpec>;
  const blocks = hubDisabled ? 'hub_disabled' : gatewayOff ? 'gateway_off' : sources.length === 0 ? 'no_source' : null;
  const config = {
    ...setupConfig,
    model_hub: { ...setupConfig.model_hub, enabled: !gatewayOff },
    capabilities: { model_hub: { enabled: !hubDisabled } },
    agent_supply_blocks: blocks ? Object.fromEntries((Object.keys(specs) as Assistant[]).map((name) => [name, blocks])) : {},
    agents: Object.fromEntries(Object.entries(setupConfig.agents).map(([name, value]) =>
      [name, name in specs && name !== 'vibey' ? { ...value, enabled: specs[name as Assistant].enabled ?? true } : value])),
  };
  // Each assistant routes its model through the first source that supplies it.
  const agentSpecs: AgentSpec[] = (Object.keys(specs) as Assistant[]).map((backend) => {
    const spec = specs[backend];
    const routes: AgentSpec['routes'] = {};
    for (const src of sources) {
      for (const model of src.models ?? []) {
        if (backend === 'claude' && src.protocol !== 'anthropic') continue;
        if (backend === 'codex' && src.protocol === 'anthropic') continue;
        routes[model.id] ??= { hops: [[src, model.id]] };
      }
    }
    return { backend, mode: spec.mode ?? 'hub', cliPresent: backend === 'vibey' || spec.cli !== 'missing', order: sources, routes };
  });
  const agents = (Object.keys(specs) as Assistant[]).map((backend) => builtinAgent(backend, specs[backend].model ?? null));
  const reads: Record<string, unknown> = {
    ...hubApi({ sources, agents: agentSpecs, runtime: runtime(health, !gatewayOff), migration }),
    '/api/config': config,
    '/api/agents': { ok: true, agents, default_agent_name: 'vibey' },
    // The providers screen starts an engine that is not running; the start answers with the engine as it is.
    '/api/models/runtime/start': ok({ runtime: runtime(health, !gatewayOff) }),
    '/api/opencode/permission-status': { ok: true, permission_allowed: !permissionRequired, config_path: '/Users/demo/.config/opencode/opencode.json' },
  };
  for (const agent of agents) reads[`/api/agents/${agent.name}`] = { ok: true, agent, default_agent_name: 'vibey' };
  for (const name of Object.keys(specs) as Assistant[]) {
    const spec = specs[name];
    reads[`/api/backend/${name}/connection`] = {
      ...connection(name, spec),
      ...(name === 'opencode' && permissionRequired ? { permission_required: true } : {}),
    };
    if (name !== 'vibey') reads[`/api/backend/${name}/runtime`] = cliRuntime(name, spec);
  }
  return { bots: [], api: { ...reads, ...api } };
}


// ── Connection dialogs ──────────────────────────────────────────────────

const claudeSignedOut: ClaudeAuthState = {
  ok: true,
  auth_mode: 'oauth',
  active_auth_mode: 'none',
  auth_managed_by: 'claude',
  oauth_store_state: 'absent',
  can_remove_oauth_credentials: false,
  has_api_key: false,
  api_key_length: 0,
  api_key_masked: null,
  has_oauth_credentials: false,
  base_url: null,
  settings_path: '/Users/demo/.claude/settings.json',
  settings_exists: false,
  settings_env_has_key: false,
  settings_env_key_length: 0,
  settings_env_key_var: null,
  credential_type: null,
  settings_env_base_url: null,
  settings_conflict: false,
};

const codexSignedOut: CodexAuthState = {
  ok: true,
  auth_mode: 'oauth',
  active_auth_mode: 'none',
  has_api_key: false,
  api_key_length: 0,
  api_key_masked: null,
  base_url: null,
  has_chatgpt_tokens: false,
  chatgpt_account: null,
  credentials_store: 'file',
  file_store_active: true,
};

const provider = (id: string, name: string, extra: Partial<OpencodeProvider> = {}): OpencodeProvider => ({
  id, name, description: '', configured: false, oauth_available: false, local: false, models: [], ...extra,
});

/** OpenCode's provider catalog: three that sign in with a subscription, the API-key brands, and a local one. */
const opencodeCatalog = {
  ok: true,
  default_provider: null,
  permission_allowed: true,
  providers: [
    provider('openai', 'OpenAI', { oauth_available: true, description: 'ChatGPT Plus / Pro', models: ['gpt-5.6-sol', 'gpt-5.6-mini'] }),
    provider('github-copilot', 'GitHub Copilot', { oauth_available: true, models: ['gpt-5.6-sol', 'claude-sonnet-5'] }),
    provider('anthropic', 'Anthropic', { oauth_available: true, description: 'Claude Pro / Max', models: ['claude-opus-5', 'claude-sonnet-5'] }),
    provider('xai', 'xAI', { models: ['grok-5'] }),
    provider('google', 'Google', { models: ['gemini-3-pro'] }),
    provider('deepseek', 'DeepSeek', { models: ['deepseek-v4', 'deepseek-v4-reasoner'] }),
    provider('alibaba', 'Alibaba', { models: ['qwen4-max'] }),
    provider('moonshotai', 'Moonshot AI', { models: ['kimi-k3'] }),
    provider('zhipuai', 'Zhipu AI', { models: ['glm-5.2'] }),
    provider('openrouter', 'OpenRouter', { models: ['openrouter/auto'] }),
    provider('ollama', 'Ollama', { local: true, models: ['qwen3:32b'] }),
  ],
};

const awaiting = (flow: string, extra: Record<string, unknown>) => ({ ok: true, flow_id: flow, state: 'awaiting_code', ...extra });

/** What each sign-in answers once started: Claude asks for the code its page returns, Codex and
 *  GitHub Copilot show a device code, OpenCode's ChatGPT sign-in returns to a local callback. */
const signIns = {
  claude: awaiting('flow_claude', { url: 'https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code&scope=user%3Ainference', callback_kind: 'code' }),
  codex: awaiting('flow_codex', { url: 'https://auth.openai.com/codex/device', device_code: '7KQF-M2XD', callback_kind: 'device' }),
  copilot: awaiting('flow_copilot', { url: 'https://github.com/login/device', device_code: 'B4D1-9E7C', callback_kind: 'device' }),
  chatgpt: awaiting('flow_chatgpt', { url: 'https://auth.openai.com/oauth/authorize?client_id=app_EMoamEEZ73f0CkXaXp7hrann&redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback', callback_kind: 'redirect' }),
};

/** The reads and sign-in answers behind the connection dialogs, for assistants that are not signed in. */
export const connectionApi: Record<string, unknown> = {
  '/api/backend/claude/auth': claudeSignedOut,
  '/api/backend/codex/auth': codexSignedOut,
  '/api/backend/opencode/providers': opencodeCatalog,
  '/api/backend/claude/auth/oauth/start': signIns.claude,
  '/api/backend/claude/auth/oauth/status/flow_claude': signIns.claude,
  '/api/backend/codex/auth/oauth/start': signIns.codex,
  '/api/backend/codex/auth/oauth/status/flow_codex': signIns.codex,
  '/api/backend/opencode/provider/github-copilot/auth/oauth/start': signIns.copilot,
  '/api/backend/opencode/auth/oauth/status/flow_copilot': signIns.copilot,
  '/api/backend/opencode/provider/openai/auth/oauth/start': signIns.chatgpt,
  '/api/backend/opencode/auth/oauth/status/flow_chatgpt': signIns.chatgpt,
};
