// Typed fixtures for the Model Hub UI while the L2 REST API is unmerged.
// Mirrors the V4 design mock story (design.pen `产品改造 V4 01r`) and the frozen
// contract example payloads. Timestamps are computed relative to "now" at fetch
// time so the 最近切换 list always renders 今天 / 昨天 correctly.
import type {
  AgentSupply,
  MigrationScan,
  Priority,
  ResolutionEvent,
  RuntimeDependency,
  Source,
} from './types';
import { CONTRACT_VERSION } from './types';
import { SUBSCRIPTION_HUB_EXPERIMENTAL } from './featureFlags';

const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;

export function buildMockSources(): Source[] {
  return [
    {
      id: 'src_claudepro1',
      kind: 'subscription',
      vendor: 'anthropic',
      display_name: 'Claude Pro 订阅',
      protocol: 'anthropic',
      base_url: null,
      supply_channel: 'native_cli',
      billing: 'monthly',
      state: { status: 'active', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: 62, month_spend_cents: null, currency: null },
      account_label: 'me@gmail.com',
      masked_credential: null,
      models: [
        { id: 'claude-opus-4-6', display_name: 'Opus 4.6', provenance: 'discovered', discovered_at: iso(-3 * HOUR) },
        { id: 'claude-sonnet-4-6', display_name: 'Sonnet 4.6', provenance: 'discovered', discovered_at: iso(-3 * HOUR) },
        { id: 'claude-haiku-4-5', display_name: 'Haiku 4.5', provenance: 'discovered', discovered_at: iso(-3 * HOUR) },
      ],
      credential_ref: null,
    },
    {
      id: 'src_chatgptplus',
      kind: 'subscription',
      vendor: 'openai',
      display_name: 'ChatGPT Plus 订阅',
      protocol: 'openai_responses',
      base_url: null,
      supply_channel: 'native_cli',
      billing: 'monthly',
      state: { status: 'active', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: 31, month_spend_cents: null, currency: null },
      account_label: 'me@gmail.com',
      masked_credential: null,
      models: [
        { id: 'gpt-5.6', display_name: 'GPT-5.6', provenance: 'discovered', discovered_at: iso(-3 * HOUR) },
        { id: 'gpt-5.6-mini', display_name: 'GPT-5.6 mini', provenance: 'discovered', discovered_at: iso(-3 * HOUR) },
      ],
      credential_ref: null,
    },
    {
      id: 'src_anthkey01',
      kind: 'api_key',
      vendor: 'anthropic',
      display_name: 'Anthropic API Key',
      protocol: 'anthropic',
      base_url: null,
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: null, month_spend_cents: 1240, currency: 'CNY' },
      account_label: null,
      masked_credential: 'sk-ant-…8f2A',
      models: [
        { id: 'claude-opus-4-6', display_name: 'Opus 4.6', provenance: 'discovered', discovered_at: iso(-6 * HOUR) },
        { id: 'claude-sonnet-4-6', display_name: 'Sonnet 4.6', provenance: 'discovered', discovered_at: iso(-6 * HOUR) },
        { id: 'claude-haiku-4-5', display_name: 'Haiku 4.5', provenance: 'discovered', discovered_at: iso(-6 * HOUR) },
      ],
      credential_ref: 'cred_anth01',
    },
    {
      id: 'src_zhipukey01',
      kind: 'api_key',
      vendor: 'zhipuai',
      display_name: '智谱 API Key',
      protocol: 'openai_compatible',
      base_url: 'https://open.bigmodel.cn/api/paas/v4',
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: null, month_spend_cents: 210, currency: 'CNY' },
      account_label: null,
      masked_credential: 'glm-…c31b',
      models: [
        { id: 'glm-5.2', display_name: 'GLM 5.2', provenance: 'discovered', discovered_at: iso(-6 * HOUR) },
        { id: 'glm-5.2-air', display_name: 'GLM 5.2 Air', provenance: 'discovered', discovered_at: iso(-6 * HOUR) },
        { id: 'glm-5-flash', display_name: 'GLM 5 Flash', provenance: 'discovered', discovered_at: iso(-6 * HOUR) },
        { id: 'glm-5.2-pro', display_name: 'GLM 5.2 Pro', provenance: 'manual', discovered_at: null },
      ],
      credential_ref: 'cred_zhipu01',
    },
    {
      id: 'src_relay9c1x',
      kind: 'api_key',
      vendor: 'custom',
      display_name: 'relay.example',
      protocol: 'openai_compatible',
      base_url: 'https://relay.example/v1',
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'cooldown', retry_at: iso(47 * MIN), detail_key: 'settings.models.source.cooldown.timeout' },
      usage: { cycle_used_pct: null, month_spend_cents: 320, currency: 'CNY' },
      account_label: null,
      masked_credential: 'key …9c1',
      models: [
        { id: 'glm-5.2-air', display_name: 'GLM 5.2 Air', provenance: 'manual', discovered_at: null },
      ],
      credential_ref: 'cred_relay01',
    },
  ];
}

export function buildMockPriority(): Priority {
  return {
    contract_version: CONTRACT_VERSION,
    order: ['src_claudepro1', 'src_chatgptplus', 'src_anthkey01', 'src_zhipukey01', 'src_relay9c1x'],
  };
}

export function buildMockAgents(): AgentSupply[] {
  return [
    {
      backend: 'claude',
      mode: 'hub',
      menu_kind: 'fixed',
      current: { model_id: 'claude-opus-4-6', source_id: 'src_claudepro1', channel: 'native_cli' },
      // Fixed-menu backends surface their full built-in id list as mappings; an
      // enabled entry is an override (frame 04), disabled = 跟随原生 (identity).
      mappings: [
        { builtin_id: 'claude-opus-4-6', target_model_id: 'glm-5.2', enabled: true },
        { builtin_id: 'claude-sonnet-4-6', target_model_id: '', enabled: false },
        { builtin_id: 'claude-haiku-4-5', target_model_id: '', enabled: false },
      ],
      menu: null,
      // Fixed-menu backends carry the server-populated built-in catalog
      // (agent-supply v1.2); the mapping drawer renders these rows.
      builtin_models: ['claude-opus-4-6', 'claude-sonnet-4-6', 'claude-haiku-4-5'],
      standard_vendors: null,
    },
    {
      backend: 'codex',
      mode: 'direct',
      menu_kind: 'fixed',
      current: null,
      mappings: [
        { builtin_id: 'gpt-5.6', target_model_id: '', enabled: false },
        { builtin_id: 'gpt-5.6-mini', target_model_id: '', enabled: false },
      ],
      menu: null,
      builtin_models: ['gpt-5.6', 'gpt-5.6-mini'],
      standard_vendors: null,
    },
    {
      backend: 'opencode',
      mode: 'hub',
      menu_kind: 'open',
      current: { model_id: 'glm-5.2', source_id: 'src_zhipukey01', channel: 'hub' },
      mappings: [],
      // Prefixed identifiers (opencode-overlay.md): provider = the SOURCE's
      // vendor (custom fallback), and only hub-channel sources materialize as
      // OpenCode providers — so every checked id is backed by a hub source in
      // buildMockSources (the two native_cli subscriptions never appear here).
      // checked = 精选 (in the picker); the rest live under 全量.
      menu: {
        view: 'featured',
        checked: [
          'anthropic/claude-opus-4-6',
          'anthropic/claude-sonnet-4-6',
          'zhipuai/glm-5.2',
          'zhipuai/glm-5.2-air',
          'zhipuai/glm-5-flash',
        ],
      },
      builtin_models: null,
      // Server mirror of STANDARD_OPENCODE_VENDOR_IDS (agent-supply v1.2), so the
      // menu / custom-model identifiers byte-match the backend's opencode_model_id.
      standard_vendors: [
        'anthropic', 'deepseek', 'github-copilot', 'google', 'groq', 'kimi',
        'minimax', 'mistral', 'moonshot', 'openai', 'openrouter', 'together',
        'xai', 'zhipuai',
      ],
    },
  ];
}

export function buildMockEvents(): ResolutionEvent[] {
  // Stored in display order (adapter-owned feed order); the UI renders as-is.
  return [
    {
      id: 'evt_a',
      ts: iso(-2 * HOUR - 11 * MIN),
      agent: 'claude',
      kind: 'switch',
      model_id: 'claude-opus-4-6',
      from_source: 'src_claudepro1',
      to_source: 'src_anthkey01',
      reason: 'quota_exhausted',
      billing_note: 'entered_metered',
      human_zh: 'Claude Code：Claude Pro 本周期额度用完 → 已切到 Anthropic API Key（按量）',
      human_en: 'Claude Code: Claude Pro cycle quota exhausted → switched to Anthropic API Key (metered)',
    },
    {
      id: 'evt_b',
      ts: iso(-38 * MIN),
      agent: 'claude',
      kind: 'recover',
      model_id: 'claude-opus-4-6',
      from_source: 'src_anthkey01',
      to_source: 'src_claudepro1',
      reason: 'recovery',
      billing_note: 'left_metered',
      human_zh: 'Claude Code：Claude Pro 额度恢复 → 已切回订阅',
      human_en: 'Claude Code: Claude Pro quota recovered → switched back to the subscription',
    },
    {
      id: 'evt_c',
      ts: iso(-1 * 24 * HOUR - 30 * MIN),
      agent: 'system',
      kind: 'cooldown',
      model_id: 'glm-5.2-air',
      from_source: 'src_relay9c1x',
      to_source: null,
      reason: 'network',
      billing_note: null,
      human_zh: 'relay.example 连续超时 → 暂停使用 1 小时，期间自动跳过',
      human_en: 'relay.example timed out repeatedly → paused for 1 hour, skipped automatically',
    },
  ];
}

export function buildMockRuntime(): RuntimeDependency {
  return {
    manifest: {
      name: 'cliproxyapi',
      version: 'v7.2.95',
      source_sha: 'f71ec0eb6776854457892452cf28c47f0d658251',
      assets: [],
    },
    status: {
      installed_version: 'v7.2.95',
      verified: true,
      listening: { host: '127.0.0.1', port: 15220 },
      health: 'ok',
      last_check: iso(-3 * MIN),
    },
  };
}

// Migration scan fixture (frame 03). Mirrors the frozen migration-scan schema
// example, adapted to the mock backends. Per spec v1.1 + the 2026-07-23 L6
// finding: API keys / base URLs → import; subscription OAuth (Claude account +
// Codex auth.json) → keep_native ALWAYS (controlled_import is deferred — adapter
// v1.2 forbids OAuth material in provision_credential). When the experimental
// flag is on, those rows only add a "re-authorize inside the hub" hint; they
// never become an import. So this fixture emits only import / keep_native.
export function buildMockMigration(): MigrationScan {
  // Subscription-OAuth rows stay native; the flag only swaps their hint line.
  const oauthNote = SUBSCRIPTION_HUB_EXPERIMENTAL
    ? 'settings.models.migration.notes.keepNativeReauthHint'
    : 'settings.models.migration.notes.keepNativeSanctioned';
  return {
    items: [
      {
        id: 'mig_claude_key',
        backend: 'claude',
        kind: 'api_key',
        masked_detail: 'Anthropic API Key · sk-…dd3c',
        proposed_action: 'import',
        selected: true,
        notes_key: 'settings.models.migration.notes.customBaseUrl',
      },
      {
        id: 'mig_claude_oauth',
        backend: 'claude',
        kind: 'oauth_native',
        masked_detail: 'Claude 账号登录（OAuth）',
        proposed_action: 'keep_native',
        selected: false,
        notes_key: oauthNote,
      },
      {
        id: 'mig_codex_auth',
        backend: 'codex',
        kind: 'oauth_native',
        masked_detail: 'ChatGPT 登录 · auth.json',
        proposed_action: 'keep_native',
        selected: false,
        notes_key: oauthNote,
      },
      {
        id: 'mig_opencode_zhipu',
        backend: 'opencode',
        kind: 'opencode_provider',
        masked_detail: '智谱 API Key · glm-…c31b',
        proposed_action: 'import',
        selected: true,
        notes_key: 'settings.models.migration.notes.fromOpencode',
      },
    ],
  };
}

// Model count a vendor's key "discovers" in the test-and-add flow (frame 06r).
export function mockDiscoveredCount(vendor: string): number {
  const table: Record<string, number> = { anthropic: 8, openai: 31, zhipuai: 12, kimi: 6, xai: 4, custom: 23 };
  return table[vendor] ?? 23;
}
