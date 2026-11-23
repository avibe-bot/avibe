// Adding models when the providers offer far more than one screen holds: 「来自你的供应商」 folds to
// one line per provider, and a provider with a long list folds once more by how its model IDs begin.
// The inventory is invented: five providers, 180 models, one of them a relay that lists the same
// family under three path prefixes.
import { BackendModelPickerDialog } from '@/components/settings/models/BackendModelPickerDialog';

import { frames } from '../../frame';
import { demoAgents, hubApi, ok } from './fixtures';

const openaiApi = ['gpt-6-astra', 'gpt-6-sol', 'gpt-6-terra', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5'];
const novaStems = ['4.1', '4.2', '4.3', '4.5', '4.6'].flatMap((version) =>
  ['', '-latest', '-fast', '-reasoning', '-non-reasoning', '-mini', '-vision'].map((variant) => `nova-${version}${variant}`));
const inventory: { id: string; name: string; models: string[] }[] = [
  {
    id: 'src_many_claude',
    name: 'Claude API',
    models: ['claude-opus-5', 'claude-opus-5-1', 'claude-opus-4-8', 'claude-opus-4-7', 'claude-sonnet-5', 'claude-sonnet-5-1', 'claude-sonnet-4-8', 'claude-haiku-5', 'claude-haiku-4-8', 'claude-fable-5', 'claude-fable-5-1'],
  },
  { id: 'src_many_openai', name: 'OpenAI API', models: openaiApi },
  {
    id: 'src_many_chatgpt',
    name: 'ChatGPT Pro',
    models: [...openaiApi.slice(0, 6), 'gpt-6-astra-codex', 'gpt-5.6-codex-mini', 'gpt-6-chat', 'gpt-image-2', 'gpt-image-2-mini', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-6-astra-pro', 'gpt-6-sol-pro'],
  },
  {
    id: 'src_many_relay',
    name: 'mega-relay',
    models: [
      ...novaStems.slice(0, 32),
      ...['nova/', 'n-labs/', 'nlabs/'].flatMap((prefix) => novaStems.map((stem) => `${prefix}${stem}`)),
      'router-auto', 'router-fast', 'embed-large', 'embed-small', 'rerank-2', 'claude-*', 'gpt-*', 'o-series*',
    ],
  },
  {
    id: 'src_many_gemini',
    name: 'Gemini CLI',
    models: ['gemini-3-flash', 'gemini-3-flash-lite', 'gemini-3-pro', 'gemini-3.5-flash', 'gemini-3.5-flash-high', 'gemini-3.5-flash-tiered', 'gemini-3.5-pro'],
  },
];

const suppliersOf = new Map<string, { source_id: string; source_name: string; model_id: string }[]>();
for (const source of inventory) {
  for (const model of source.models) {
    suppliersOf.set(model, [...(suppliersOf.get(model) ?? []), { source_id: source.id, source_name: source.name, model_id: model }]);
  }
}
const api = {
  ...hubApi({ agents: demoAgents }),
  // The search also asks the model catalog; it knows nothing more here.
  [`/api/models/catalog/models-dev?query=${encodeURIComponent('opus 4')}&refresh=if-stale`]: ok({ matches: [] }),
  '/api/models/agents/vibey/models/candidates': ok({
    candidates: {
      builtin: [],
      providers: [...suppliersOf].map(([id, suppliers]) => ({ id, display_name: null, reasoning_efforts: [], suppliers, origin: 'provider' })),
      in_list: [],
    },
  }),
};

const none = new Set<string>();
const render = () => <BackendModelPickerDialog open backend="vibey" listedIds={none} onCancel={() => {}} onAdd={() => {}} onCustom={() => {}} />;
const provider = (name: string) => `.model-hub-picker-source-head:has-text("${name}")`;
const segment = (label: string) => `.model-hub-picker-segment-head:has-text("${label}")`;
const desktop = { width: 1000, height: 820, status: 'shipped' as const, fixtures: { api }, render };
const phone = { width: 390, height: 844, status: 'shipped' as const, fixtures: { api }, render };

const designFrames = frames([
  { name: 'folded', title: '添加模型 · 180 个模型，每个供应商一行', ...desktop },
  {
    name: 'provider-open',
    title: '添加模型 · 展开一个供应商并选了几个',
    ...desktop,
    click: [provider('Gemini CLI'), 'text="gemini-3.5-flash-tiered"', 'text="gemini-3-pro"', provider('Claude API'), 'text="claude-fable-5-1"', provider('Claude API')],
  },
  { name: 'search', title: '添加模型 · 搜索时只留有结果的供应商', ...desktop, click: [{ fill: '.model-hub-catalog-search input', text: 'opus 4' }], wait: 400 },
  { name: 'large-provider', title: '添加模型 · 145 个模型的供应商再分一层', ...desktop, click: [provider('mega-relay')] },
  {
    name: 'large-provider-open',
    title: '添加模型 · 展开其中一组',
    ...desktop,
    height: 1100,
    click: [provider('mega-relay'), segment('nova-'), 'text="nova-4.1-latest"', 'text="nova-4.2"'],
  },
  { name: 'folded-phone', title: '添加模型 · 180 个模型 · 手机', ...phone, click: [provider('ChatGPT Pro')] },
  { name: 'large-provider-phone', title: '添加模型 · 145 个模型的供应商 · 手机', ...phone, click: [provider('mega-relay')] },
]);

export default designFrames;
