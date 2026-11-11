// Adding a model where the models are listed.
//
// Every model list on Settings · Models ends in 添加模型 (approved, not shipped yet: it is drawn
// with the kit row), and the dialog it opens is the shipped picker: it searches the providers and
// the model catalog together, and a search that finds nothing adds what was typed as a custom model.
import { BackendModelPickerDialog } from '@/components/settings/models/BackendModelPickerDialog';
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';
import type { ModelsDevMatch, Source } from '@/components/settings/models/types';

import { frames } from '../../frame';
import { ModelListAddRow } from '../../kit/model-add';
import { IntoPage } from '../../shell/IntoPage';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { demoAgents, hubApi, ok, relayClaude, relayGpt, deepseek } from './fixtures';

const page = settingsPage('models', (
  <>
    <SettingsModelsPage />
    {/* The model list of each backend Model Hub serves. */}
    <IntoPage selector="[data-agent-backend] > div.space-y-2"><ModelListAddRow /></IntoPage>
  </>
));

const supplier = (source: Source, model: string) => ({ source_id: source.id, source_name: source.display_name, model_id: model });
const candidate = (id: string, display: string | null, suppliers: ReturnType<typeof supplier>[], origin: 'builtin' | 'provider' = 'provider') => ({
  id, display_name: display, reasoning_efforts: [], suppliers, origin,
});

/** What Vibey can add: two of its own, and what the relays supply. Fable 5 is already in its list. */
const vibeyCandidates = ok({
  candidates: {
    builtin: [
      candidate('claude-opus-5-5', 'Claude Opus 5.5', [], 'builtin'),
      candidate('claude-haiku-5-5', 'Claude Haiku 5.5', [], 'builtin'),
    ],
    providers: [
      candidate('claude-fable-5-1', 'Claude Fable 5.1', [supplier(relayClaude, 'claude-fable-5-1')]),
      candidate('claude-opus-5', 'Claude Opus 5', [supplier(relayClaude, 'claude-opus-5')]),
      candidate('claude-sonnet-5', 'Claude Sonnet 5', [supplier(relayClaude, 'claude-sonnet-5')]),
      candidate('gpt-6-astra', 'GPT-6 Astra', [supplier(relayGpt, 'gpt-6-astra')]),
      candidate('gpt-6.1-terra', 'GPT-6.1 Terra', [supplier(relayGpt, 'gpt-6.1-terra')]),
      candidate('deepseek-v4', null, [supplier(deepseek, 'deepseek-v4')]),
    ],
    in_list: [candidate('claude-fable-5', 'Claude Fable 5', [supplier(relayClaude, 'claude-fable-5')])],
  },
});

/** The catalog's answer for 「Fable 5.2」: the provider lists 5 and 5.1, the catalog also knows 5.2. */
const fable = (id: string, name: string): ModelsDevMatch => ({
  provider_id: 'anthropic', provider_name: 'Anthropic', model_id: id, models_dev_id: `anthropic/${id}`, display_name: name,
  context_window: 400_000, max_output_tokens: 64_000, input_modalities: ['text', 'image'], output_modalities: ['text'],
  supports_tools: true, supports_reasoning: true, reasoning_efforts: ['low', 'medium', 'high'], native_protocol: 'anthropic', first_party: true,
});
const lookup = (query: string) => `/api/models/catalog/models-dev?query=${encodeURIComponent(query)}&refresh=if-stale`;

const api = {
  ...hubApi({ agents: demoAgents }),
  '/api/models/agents/vibey/models/candidates': vibeyCandidates,
  [lookup('Fable 5.2')]: ok({ matches: [fable('claude-fable-5-2', 'Claude Fable 5.2'), fable('claude-fable-5', 'Claude Fable 5')] }),
  [lookup('my-finetune-v3')]: ok({ matches: [] }),
};

const listed = new Set(['claude-fable-5']);
const dialog = (query = '', choose: 'many' | 'one' = 'many') => () => (
  <BackendModelPickerDialog open backend="vibey" listedIds={listed} initialQuery={query} choose={choose}
    onCancel={() => {}} onAdd={() => {}} onCustom={choose === 'many' ? () => {} : undefined} />
);
/** The catalog is asked once typing pauses; a frame waits it out. */
const searched = 900;

const designFrames = frames([
  {
    name: 'routes-entry',
    title: '模型网关 · 每个后端的模型列表以「添加模型」结尾',
    width: 1440,
    height: 1900,
    status: 'approved',
    route: '/settings/models',
    fixtures: settingsFixtures(hubApi({ agents: demoAgents })),
    render: page,
  },
  {
    name: 'dialog',
    title: '添加模型 · 打开时',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    render: dialog(),
  },
  {
    name: 'search-catalog',
    title: '添加模型 · 供应商没列出、模型目录里有',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    wait: searched,
    render: dialog('Fable 5.2'),
  },
  {
    name: 'search-none',
    title: '添加模型 · 哪里都搜不到',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    wait: searched,
    render: dialog('my-finetune-v3'),
  },
  {
    name: 'setup-choose',
    title: '设置向导 · 为 Vibey 选模型时搜索',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    wait: searched,
    render: dialog('Fable 5.2', 'one'),
  },
  {
    name: 'search-catalog-phone',
    title: '添加模型 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api },
    wait: searched,
    render: dialog('Fable 5.2'),
  },
]);

export default designFrames;
