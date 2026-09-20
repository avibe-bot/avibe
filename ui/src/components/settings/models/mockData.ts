// Typed source fixtures retained only for the source schema contract test.
//
// Every model carries `reasoning_efforts_source`, and between them the five
// sources spell out all four rungs of the provenance ladder: `upstream`,
// `catalog`, `user`, and an explicit `null`. Each list matches what its rung
// really produces: exact catalog rows, protocol-family upstream defaults,
// arbitrary user declarations, or an empty undeclared list.
import type { Source } from './types';

const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

export function buildMockSources(): Source[] {
  return [
    {
      id: 'src_claudepro1',
      created_at: iso(-30 * DAY),
      last_discovered_at: iso(-3 * HOUR),
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
        { id: 'claude-opus-4-6', display_name: 'Opus 4.6', origin: 'discovered', reasoning_efforts: ['low', 'medium', 'high', 'max'], reasoning_efforts_source: 'catalog', discovered_at: iso(-3 * HOUR) },
        { id: 'claude-sonnet-4-6', display_name: 'Sonnet 4.6', origin: 'discovered', reasoning_efforts: ['low', 'medium', 'high', 'max'], reasoning_efforts_source: 'catalog', discovered_at: iso(-3 * HOUR) },
        { id: 'claude-haiku-4-5', display_name: 'Haiku 4.5', origin: 'discovered', reasoning_efforts: ['low', 'medium', 'high'], reasoning_efforts_source: 'catalog', discovered_at: iso(-3 * HOUR) },
      ],
      credential_ref: null,
    },
    {
      id: 'src_chatgptplus',
      created_at: iso(-20 * DAY),
      last_discovered_at: iso(-3 * HOUR),
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
        { id: 'gpt-5.6', display_name: 'GPT-5.6', origin: 'discovered', reasoning_efforts: ['minimal', 'low', 'medium', 'high', 'xhigh'], reasoning_efforts_source: 'upstream', discovered_at: iso(-3 * HOUR) },
        { id: 'gpt-5.6-mini', display_name: 'GPT-5.6 mini', origin: 'discovered', reasoning_efforts: ['minimal', 'low', 'medium', 'high', 'xhigh'], reasoning_efforts_source: 'upstream', discovered_at: iso(-3 * HOUR) },
      ],
      credential_ref: null,
    },
    {
      id: 'src_anthkey01',
      created_at: iso(-10 * DAY),
      last_discovered_at: iso(-6 * HOUR),
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
        { id: 'claude-opus-4-6', display_name: 'Opus 4.6', origin: 'discovered', reasoning_efforts: ['low', 'medium', 'high', 'xhigh', 'max'], reasoning_efforts_source: 'upstream', discovered_at: iso(-6 * HOUR) },
        { id: 'claude-sonnet-4-6', display_name: 'Sonnet 4.6', origin: 'discovered', reasoning_efforts: ['low', 'medium', 'high', 'xhigh', 'max'], reasoning_efforts_source: 'upstream', discovered_at: iso(-6 * HOUR) },
        { id: 'claude-haiku-4-5', display_name: 'Haiku 4.5', origin: 'discovered', reasoning_efforts: ['low', 'medium', 'high', 'xhigh', 'max'], reasoning_efforts_source: 'upstream', discovered_at: iso(-6 * HOUR) },
      ],
      credential_ref: 'cred_anth01',
    },
    {
      id: 'src_zhipukey01',
      created_at: iso(-4 * DAY),
      last_discovered_at: iso(-6 * HOUR),
      kind: 'api_key',
      vendor: 'zhipuai',
      display_name: '智谱 API Key',
      protocol: 'openai_chat',
      base_url: 'https://open.bigmodel.cn/api/paas/v4',
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: null, month_spend_cents: 210, currency: 'USD' },
      account_label: null,
      masked_credential: 'glm-…c31b',
      models: [
        { id: 'glm-5.2', display_name: 'GLM 5.2', origin: 'discovered', reasoning_efforts: ['low', 'high'], reasoning_efforts_source: 'user', discovered_at: iso(-6 * HOUR) },
        { id: 'glm-5.2-air', display_name: 'GLM 5.2 Air', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null, discovered_at: iso(-6 * HOUR) },
        { id: 'glm-5-flash', display_name: 'GLM 5 Flash', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null, discovered_at: iso(-6 * HOUR) },
        { id: 'glm-5.2-pro', display_name: 'GLM 5.2 Pro', origin: 'manual', reasoning_efforts: ['medium', 'high'], reasoning_efforts_source: 'user', discovered_at: null },
      ],
      credential_ref: 'cred_zhipu01',
    },
    {
      id: 'src_relay9c1x',
      // Older than the 智谱 key on purpose: 「V6 01」 draws Codex's recommended
      // chain as ChatGPT Plus › relay.example › 智谱 API Key, and under §4.2 an
      // api_key's place in that chain is its created_at.
      created_at: iso(-6 * DAY),
      last_discovered_at: null,
      kind: 'api_key',
      vendor: 'custom',
      display_name: 'relay.example',
      protocol: 'openai_chat',
      base_url: 'https://relay.example/v1',
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'cooldown', retry_at: iso(47 * MIN), detail_key: 'models.source.cooldown.server_error' },
      usage: { cycle_used_pct: null, month_spend_cents: 320, currency: 'USD' },
      account_label: null,
      masked_credential: 'key …9c1',
      models: [
        { id: 'glm-5.2-air', display_name: 'GLM 5.2 Air', origin: 'manual', reasoning_efforts: [], reasoning_efforts_source: null, discovered_at: null },
      ],
      credential_ref: 'cred_relay01',
    },
  ];
}
