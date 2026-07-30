// Typed fixtures for the Model Hub UI while the L2 REST API is unmerged.
// Mirrors the V6 design mock story (design.pen `产品改造 V6 01`) and the frozen
// v3 contract example payloads. Timestamps are computed relative to "now" at
// fetch time so the 最近切换 list always renders 今天 / 昨天 correctly.
//
// The two exported predicates at the bottom (`mockEligibility`,
// `mockRecommendedOrder`) implement spec §4.4 and §4.2. They belong to the FAKE
// SERVER, not to the UI: production reads `AgentSources.eligibility` /
// `.order` off the wire and never re-derives either. They live here so the
// fixture and the MockStore share one implementation.
import type {
  AgentBackend,
  AgentSupply,
  MigrationScan,
  ResolutionEvent,
  RuntimeDependency,
  Source,
  SourceEligibility,
} from './types';
import { SUBSCRIPTION_HUB_EXPERIMENTAL } from './featureFlags';

const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

export function buildMockSources(): Source[] {
  return [
    {
      id: 'src_claudepro1',
      created_at: iso(-30 * DAY),
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
      created_at: iso(-20 * DAY),
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
      created_at: iso(-10 * DAY),
      kind: 'api_key',
      vendor: 'anthropic',
      display_name: 'Anthropic API Key',
      protocol: 'anthropic',
      base_url: null,
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: null, month_spend_cents: 1240, currency: 'USD' },
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
      created_at: iso(-4 * DAY),
      kind: 'api_key',
      vendor: 'zhipuai',
      display_name: '智谱 API Key',
      protocol: 'openai_compatible',
      base_url: 'https://open.bigmodel.cn/api/paas/v4',
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: null, month_spend_cents: 210, currency: 'USD' },
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
      // Older than the 智谱 key on purpose: 「V6 01」 draws Codex's recommended
      // chain as ChatGPT Plus › relay.example › 智谱 API Key, and under §4.2 an
      // api_key's place in that chain is its created_at.
      created_at: iso(-6 * DAY),
      kind: 'api_key',
      vendor: 'custom',
      display_name: 'relay.example',
      protocol: 'openai_compatible',
      base_url: 'https://relay.example/v1',
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'cooldown', retry_at: iso(47 * MIN), detail_key: 'models.source.cooldown.timeout' },
      usage: { cycle_used_pct: null, month_spend_cents: 320, currency: 'USD' },
      account_label: null,
      masked_credential: 'key …9c1',
      models: [
        { id: 'glm-5.2-air', display_name: 'GLM 5.2 Air', provenance: 'manual', discovered_at: null },
      ],
      credential_ref: 'cred_relay01',
    },
  ];
}

// The V6 01 story: Claude Code and Codex are on the hub (「2 个 Agent 已接入中枢」),
// OpenCode still runs its native config (直连). Claude Code owns a 自定义 subset
// that leaves 智谱 out — which is what makes the drawer's 未启用 section non-empty
// in frame V6 02; Codex 跟随推荐, so its order IS §4.2's output.
export function buildMockAgents(sources: Source[] = buildMockSources()): AgentSupply[] {
  const claudeOrder = ['src_claudepro1', 'src_anthkey01', 'src_relay9c1x'];
  return [
    {
      backend: 'claude',
      mode: 'hub',
      menu_kind: 'fixed',
      selected_by_agent: null,
      selected_model_id: 'claude-opus-4-6',
      current: { model_id: 'claude-opus-4-6', source_id: 'src_claudepro1', channel: 'native_cli' },
      sources: {
        policy: 'custom',
        order: claudeOrder,
        eligibility: mockEligibility(sources, 'claude'),
      },
      // Serving from the head of the chain for the CURRENT selection, and no
      // member of that chain is blocked — relay.example is cooling, but it does
      // not supply Opus at all, so it is not a member and cannot degrade it.
      supply_status: 'ok',
      // claude-haiku-4-5 is mapped onto glm-5.2, which only 智谱 supplies — and
      // 智谱 is 未启用 here. Depth 0 is the honest answer, and AC-9's Case A: a
      // menu-model gap that must NOT render any Agent as interrupted.
      model_supply: [
        { model_id: 'claude-opus-4-6', chain_length: 2 },
        { model_id: 'claude-sonnet-4-6', chain_length: 2 },
        { model_id: 'claude-haiku-4-5', chain_length: 0 },
      ],
      named_agents: [
        { name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' },
        { name: 'pm', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' },
      ],
      // Fixed-menu backends surface their full built-in id list as mappings; an
      // enabled entry is an override (frame 04), disabled = 跟随原生 (identity).
      mappings: [
        { builtin_id: 'claude-opus-4-6', target_model_id: '', enabled: false },
        { builtin_id: 'claude-sonnet-4-6', target_model_id: '', enabled: false },
        { builtin_id: 'claude-haiku-4-5', target_model_id: 'glm-5.2', enabled: true },
      ],
      menu: null,
      // Fixed-menu backends carry the server-populated built-in catalog
      // (agent-supply v1.2); the mapping drawer renders these rows.
      builtin_models: ['claude-opus-4-6', 'claude-sonnet-4-6', 'claude-haiku-4-5'],
      standard_vendors: null,
    },
    {
      backend: 'codex',
      mode: 'hub',
      menu_kind: 'fixed',
      selected_by_agent: 'codex',
      selected_model_id: 'gpt-5.6',
      current: { model_id: 'gpt-5.6', source_id: 'src_chatgptplus', channel: 'native_cli' },
      sources: {
        policy: 'follow',
        // Recomputed on every read: 跟随推荐 means a new source joins by itself.
        order: mockRecommendedOrder(sources, 'codex'),
        eligibility: mockEligibility(sources, 'codex'),
      },
      supply_status: 'ok',
      model_supply: [
        { model_id: 'gpt-5.6', chain_length: 1 },
        { model_id: 'gpt-5.6-mini', chain_length: 1 },
      ],
      named_agents: [{ name: 'codex', effective_model_id: 'gpt-5.6', supply_status: 'ok' }],
      mappings: [
        { builtin_id: 'gpt-5.6', target_model_id: '', enabled: false },
        { builtin_id: 'gpt-5.6-mini', target_model_id: '', enabled: false },
      ],
      menu: null,
      builtin_models: ['gpt-5.6', 'gpt-5.6-mini'],
      standard_vendors: null,
    },
    {
      // 直连: the CLI runs its own native config. Every hub-side projection is
      // null by contract — no order, no chain, no probe (AC-7's gate).
      backend: 'opencode',
      mode: 'direct',
      menu_kind: 'open',
      selected_by_agent: null,
      selected_model_id: null,
      current: null,
      sources: null,
      supply_status: null,
      model_supply: null,
      named_agents: [{ name: 'opencode', effective_model_id: null, supply_status: null }],
      mappings: [],
      // Prefixed identifiers (opencode-overlay.md): provider = the SOURCE's
      // vendor (custom fallback). Retained across the mode switch — it is stored
      // config, not live state, and applies again the moment OpenCode rejoins.
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
      severity: 'info',
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
      severity: 'info',
      human_zh: 'Claude Code：Claude Pro 额度恢复 → 已切回订阅',
      human_en: 'Claude Code: Claude Pro quota recovered → switched back to the subscription',
    },
    {
      id: 'evt_c',
      ts: iso(-1 * DAY - 30 * MIN),
      agent: 'system',
      kind: 'cooldown',
      model_id: 'glm-5.2-air',
      from_source: 'src_relay9c1x',
      to_source: null,
      reason: 'network',
      billing_note: null,
      severity: 'info',
      human_zh: 'relay.example 连续超时 → 暂停使用 1 小时，期间自动跳过',
      human_en: 'relay.example timed out repeatedly → paused for 1 hour, skipped automatically',
    },
    // AC-18's render-time 「已删除」 case: a retained event whose endpoint no
    // longer resolves. The recorded sentence still names the source, because it
    // was composed when the source existed — that string IS the snapshot.
    {
      id: 'evt_d',
      ts: iso(-2 * DAY - 4 * HOUR),
      agent: 'codex',
      kind: 'needs_action',
      model_id: 'gpt-5.6',
      from_source: 'src_oldkey0099',
      to_source: null,
      reason: 'credential_revoked',
      billing_note: null,
      severity: 'action_required',
      human_zh: 'Codex：旧 OpenAI API Key 凭证已失效 → 已跳过，需要重新授权',
      human_en: 'Codex: the old OpenAI API Key credential was revoked → skipped, re-authorization needed',
    },
    // A history tail, so 「查看全部」 crosses a page boundary in the mock. With
    // four rows the cursor path was unreachable in dev — the very case the feed's
    // pagination exists for could only be seen against a real server.
    // Deep enough that the feed needs THREE pages at the page size 最近切换 asks
    // for (20): expanding 查看全部 pulls the second page on its own, so anything
    // shallower exhausts the cursor before 查看更多 is ever clickable — which is
    // how the button shipped unexercised in the first place.
    ...olderHistory(45),
  ];
}

/** Filler older events, oldest last, ids stable so `before` cursors are stable. */
function olderHistory(count: number): ResolutionEvent[] {
  return Array.from({ length: count }, (_, i) => {
    const n = i + 1;
    const cooling = n % 3 === 0;
    return {
      id: `evt_h${String(n).padStart(2, '0')}`,
      ts: iso(-3 * DAY - i * (5 * HOUR + 20 * MIN)),
      agent: 'claude',
      kind: cooling ? 'cooldown' : 'switch',
      model_id: 'claude-opus-4-6',
      from_source: cooling ? 'src_relay9c1x' : 'src_claudepro1',
      to_source: cooling ? null : 'src_anthkey01',
      reason: cooling ? 'rate_limited' : 'quota_exhausted',
      billing_note: cooling ? null : 'entered_metered',
      severity: 'info',
      human_zh: cooling
        ? 'relay.example 触发限流 → 暂停使用 10 分钟，期间自动跳过'
        : 'Claude Code：Claude Pro 本周期额度用完 → 已切到 Anthropic API Key（按量）',
      human_en: cooling
        ? 'relay.example hit a rate limit → paused for 10 minutes, skipped automatically'
        : 'Claude Code: Claude Pro cycle quota exhausted → switched to Anthropic API Key (metered)',
    } satisfies ResolutionEvent;
  });
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

// ── Fake-server predicates (spec §4.4 + §4.2) ───────────────────────────
// The vendor→client binding an eligible subscription must satisfy. OpenCode has
// no subscription channel at all, so it has no own vendor.
const OWN_VENDOR: Record<AgentBackend, string | null> = {
  claude: 'anthropic',
  codex: 'openai',
  opencode: null,
};

/** §4.4 — server-authoritative eligibility, one row per source. api_key sources
 *  serve every backend; a subscription is bound to its own client, and the
 *  hub-held channel additionally needs the flag plus explicit consent. */
export function mockEligibility(sources: Source[], backend: AgentBackend): SourceEligibility[] {
  return sources.map((s): SourceEligibility => {
    if (s.kind === 'api_key') return { source_id: s.id, eligible: true, reason_key: null };
    if (backend === 'opencode') {
      return { source_id: s.id, eligible: false, reason_key: 'models.eligibility.opencode_api_key_only' };
    }
    if (s.vendor !== OWN_VENDOR[backend]) {
      return { source_id: s.id, eligible: false, reason_key: 'models.eligibility.subscription_wrong_client' };
    }
    if (s.supply_channel === 'hub' && !(SUBSCRIPTION_HUB_EXPERIMENTAL && s.experimental_consent_at)) {
      return { source_id: s.id, eligible: false, reason_key: 'models.eligibility.consent_required' };
    }
    return { source_id: s.id, eligible: true, reason_key: null };
  });
}

// An absent created_at sorts before every present one (types.ts: the stamp may
// be null on rows persisted before the field existed).
const createdKey = (s: Source) => s.created_at ?? '';

/** §4.2 — the deterministic recommendation, exhaustive over eligible sources:
 *  (1) the backend's own-vendor subscription, native_cli before a hub-held one;
 *  (2) every eligible api_key by created_at ascending; (3) id ascending as the
 *  tie-break anywhere. No health, latency, cost or usage input. */
export function mockRecommendedOrder(sources: Source[], backend: AgentBackend): string[] {
  const eligible = new Set(
    mockEligibility(sources, backend).filter((e) => e.eligible).map((e) => e.source_id),
  );
  const pool = sources.filter((s) => eligible.has(s.id));
  const byId = (a: Source, b: Source) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const subs = pool
    .filter((s) => s.kind === 'subscription')
    .sort((a, b) => {
      const rank = (s: Source) => (s.supply_channel === 'native_cli' ? 0 : 1);
      return rank(a) - rank(b) || byId(a, b);
    });
  const keys = pool
    .filter((s) => s.kind === 'api_key')
    .sort((a, b) => {
      const ca = createdKey(a);
      const cb = createdKey(b);
      return (ca < cb ? -1 : ca > cb ? 1 : 0) || byId(a, b);
    });
  return [...subs, ...keys].map((s) => s.id);
}
