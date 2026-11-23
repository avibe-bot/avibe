// A backend's model menu (管理模型): the shipped BackendModelCatalogDialog, its picker for adding
// models providers supply, and its editor for one model.
// The custom-model-* frames past the first draw where a custom model's parameters come from: the
// model catalog's answer for the ID typed (the same model, the closest one, or none), the offer
// when an edited ID answers to another model, and 「更换」, the catalog search that applies one.
import { BackendModelCatalogDialog } from '@/components/settings/models/BackendModelCatalogDialog';
import { BackendModelEditorDialog } from '@/components/settings/models/BackendModelEditorDialog';
import { BackendModelPickerDialog } from '@/components/settings/models/BackendModelPickerDialog';
import type { AgentBackend, ModelsDevMatch, Source } from '@/components/settings/models/types';

import { frames } from '../../frame';
import { AfterMount } from '../../shell/AfterMount';
import { backendModel, deepseek, demoAgents, demoSources, hubApi, ok, relayClaude, relayGpt, type AgentSpec } from './fixtures';

const sourceNames = Object.fromEntries(demoSources.map((s) => [s.id, s.display_name]));

const catalogWrite = { pending: false, track: () => new Promise<void>(() => {}) };
const catalog = (backend: AgentBackend) => () => (
  <BackendModelCatalogDialog
    open
    backend={backend}
    canReadSources
    sourceNames={sourceNames}
    onClose={() => {}}
    onSaved={() => {}}
    onObserved={() => {}}
    catalogWrite={catalogWrite}
  />
);

// Claude Code's menu: the built-in models with their reasoning efforts, plus one a provider added.
const claudeEfforts = ['low', 'medium', 'high', 'max'];
// Vibey with a list longer than the dialog: eleven models, so the list scrolls under the footer.
const vibeyLong = ['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5', 'claude-opus-4-8', 'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5', 'deepseek-v4', 'deepseek-v4-reasoner'];
const menuAgents: AgentSpec[] = demoAgents.map((spec) => spec.backend === 'vibey' ? {
  ...spec,
  catalog: vibeyLong.map((id) => backendModel(id, { origin: 'provider' })),
} : spec.backend !== 'claude' ? spec : {
  ...spec,
  catalog: [
    backendModel('default', { display_name: 'Default', locked: true, routeable: false }),
    backendModel('claude-opus-5', { display_name: 'Opus 5', reasoning_efforts: claudeEfforts, supports_reasoning: true, context_window: 1_000_000, max_output_tokens: 128_000 }),
    backendModel('claude-sonnet-5', { display_name: 'Sonnet 5', reasoning_efforts: claudeEfforts, supports_reasoning: true, context_window: 1_000_000, max_output_tokens: 64_000 }),
    backendModel('claude-haiku-5', { display_name: 'Haiku 5', reasoning_efforts: ['low', 'medium', 'high'], supports_reasoning: true, context_window: 200_000 }),
    backendModel('claude-opus-4-8', { origin: 'provider', reasoning_efforts: claudeEfforts, supports_reasoning: true }),
  ],
});

const supplier = (source: Source, model: string) => ({ source_id: source.id, source_name: source.display_name, model_id: model });
const candidate = (id: string, suppliers: ReturnType<typeof supplier>[], protocol: 'openai_responses' | 'anthropic' = 'openai_responses') => ({
  id, display_name: null, reasoning_efforts: [], suppliers, origin: 'provider', native_protocol: protocol,
});

/** What the picker offers OpenCode: models its providers supply that the menu does not hold yet. */
const opencodeCandidates = ok({
  candidates: {
    builtin: [],
    providers: [
      candidate('claude-opus-5', [supplier(relayClaude, 'claude-opus-5')], 'anthropic'),
      candidate('claude-haiku-5', [supplier(relayClaude, 'claude-haiku-5')], 'anthropic'),
      candidate('claude-opus-4-8', [supplier(relayClaude, 'claude-opus-4-8')], 'anthropic'),
      candidate('gpt-6-astra', [supplier(relayGpt, 'gpt-6-astra')]),
      candidate('gpt-5.6-terra', [supplier(relayGpt, 'gpt-5.6-terra')]),
      candidate('gpt-5.6-luna', [supplier(relayGpt, 'gpt-5.6-luna')]),
      candidate('gpt-5.5', [supplier(relayGpt, 'gpt-5.5')]),
    ],
    in_list: [
      candidate('deepseek-v4', [supplier(deepseek, 'deepseek-v4')]),
      candidate('deepseek-v4-reasoner', [supplier(deepseek, 'deepseek-v4-reasoner')]),
      candidate('claude-sonnet-5', [supplier(relayClaude, 'claude-sonnet-5')], 'anthropic'),
      candidate('gpt-5.6-sol', [supplier(relayGpt, 'gpt-5.6-sol')]),
    ],
  },
});

/** Codex before any provider supplies more: only its built-in models to choose from. */
const codexBuiltinOnly = ok({
  candidates: {
    builtin: [
      { id: 'gpt-6-astra-codex', display_name: 'GPT-6 Astra Codex', reasoning_efforts: ['low', 'medium', 'high', 'xhigh'], suppliers: [], origin: 'builtin' },
      { id: 'gpt-5.6-codex-mini', display_name: 'GPT-5.6 Codex mini', reasoning_efforts: ['low', 'medium', 'high'], suppliers: [], origin: 'builtin' },
    ],
    providers: [],
    in_list: [],
  },
});

const api = {
  ...hubApi({ agents: menuAgents }),
  '/api/models/agents/opencode/models/candidates': opencodeCandidates,
  '/api/models/agents/codex/models/candidates': codexBuiltinOnly,
};

const picker = (backend: AgentBackend, listed: ReadonlySet<string>) => () => (
  <BackendModelPickerDialog open backend={backend} listedIds={listed} onCancel={() => {}} onAdd={() => {}} onCustom={() => {}} />
);

const editing = backendModel('claude-opus-4-8', { origin: 'provider', reasoning_efforts: claudeEfforts, supports_reasoning: true, supports_tools: true, context_window: 1_000_000, max_output_tokens: 128_000 });

const opencodeListed = new Set(['deepseek-v4', 'deepseek-v4-reasoner', 'claude-sonnet-5', 'gpt-5.6-sol']);
const codexListed = new Set(['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini']);
const claudeTaken = new Set(['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5']);

// A custom model and the catalog: the ID is the user's; the catalog fills a blank form with what
// it knows about the model that ID answers to, and after that only offers.
const fable51: ModelsDevMatch = {
  match_kind: 'exact',
  provider_id: 'anthropic', provider_name: 'Anthropic', model_id: 'claude-fable-5-1', models_dev_id: 'anthropic/claude-fable-5-1', display_name: 'Claude Fable 5.1',
  context_window: 400_000, max_output_tokens: 64_000, input_modalities: ['text', 'image'], output_modalities: ['text'],
  supports_tools: true, supports_reasoning: true, reasoning_efforts: ['low', 'medium', 'high'], native_protocol: 'anthropic', first_party: true,
};
const fable5: ModelsDevMatch = { ...fable51, model_id: 'claude-fable-5', models_dev_id: 'anthropic/claude-fable-5', display_name: 'Claude Fable 5', context_window: 200_000 };
const fableLite: ModelsDevMatch = { ...fable51, model_id: 'claude-fable-5-1-lite', models_dev_id: 'anthropic/claude-fable-5-1-lite', display_name: 'Claude Fable 5.1 Lite', context_window: 200_000, max_output_tokens: 32_000 };
const exactId = 'claude-fable-5-1';
// A provider's name for a variant: no model is registered under it, so the catalog answers with its base.
const variantId = 'claude-fable-5-1-thinking';
const unknownId = 'my-private-model-v2';
const lookup = (query: string) => `/api/models/catalog/models-dev?query=${encodeURIComponent(query)}&refresh=if-stale`;
const customApi = {
  ...api,
  // The edit-model frame's row: the catalog is asked about a saved row's ID too, and here has nothing to add.
  [lookup('claude-opus-4-8')]: ok({ matches: [] }),
  [lookup(exactId)]: ok({ matches: [fable51, fableLite, fable5] }),
  [lookup(variantId)]: ok({ matches: [{ ...fable51, match_kind: 'closest', model_id: variantId }] }),
  [lookup(unknownId)]: ok({ matches: [] }),
  [lookup('claude-fable-5')]: ok({ matches: [fable5, fable51, fableLite] }),
  [lookup('fable')]: ok({ matches: [fable51, fableLite, fable5] }),
};
const vibeyTaken = new Set(['claude-opus-5', 'claude-sonnet-5']);
/** Opened after mount, as the picker opens it, so the read it starts is not dropped by StrictMode's remount. */
const custom = (seedId?: string) => () => (
  <AfterMount>
    {(open) => <BackendModelEditorDialog open={open} backend="vibey" model={null} seedId={seedId} takenIds={vibeyTaken} effortSuggestions={['low', 'medium', 'high']} onCancel={() => {}} onCommit={() => {}} />}
  </AfterMount>
);
const idField = 'role=textbox[name="{t:settings.models.gateway.modelEditor.id.label}"]';
const change = 'text={t:settings.models.gateway.modelEditor.source.change}';
/** The reads behind the ID and behind the catalog search are debounced. */
const customFrame = { width: 1000, height: 900, themes: ['light'] as ('light' | 'dark')[], status: 'shipped' as const, fixtures: { api: customApi }, wait: 900 };

const designFrames = frames([
  {
    name: 'claude',
    title: '管理模型 · Claude Code',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    render: catalog('claude'),
  },
  {
    name: 'opencode',
    title: '管理模型 · OpenCode',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    render: catalog('opencode'),
  },
  {
    name: 'long-list',
    title: '管理模型 · 列表比窗口长（Vibey，11 个模型）',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    // Scrolled to its end: clicking the last row's name brings it into view, and nothing else.
    click: ['text=deepseek-v4-reasoner'],
    render: catalog('vibey'),
  },
  {
    name: 'phone',
    title: '管理模型 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api },
    render: catalog('codex'),
  },
  {
    name: 'add-models',
    title: '添加模型 · 从供应商选择（OpenCode）',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    render: picker('opencode', opencodeListed),
  },
  {
    name: 'add-models-no-providers',
    title: '添加模型 · 供应商还没有更多模型（Codex）',
    width: 1000,
    height: 820,
    status: 'shipped',
    fixtures: { api },
    render: picker('codex', codexListed),
  },
  {
    name: 'add-models-phone',
    title: '添加模型 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api },
    render: picker('opencode', opencodeListed),
  },
  {
    name: 'edit-model',
    title: '编辑模型 · claude-opus-4-8',
    width: 1000,
    height: 900,
    status: 'shipped',
    fixtures: { api: customApi },
    render: () => (
      <BackendModelEditorDialog
        open
        backend="claude"
        model={editing}
        takenIds={claudeTaken}
        effortSuggestions={claudeEfforts}
        onCancel={() => {}}
        onCommit={() => {}}
      />
    ),
  },
  {
    name: 'custom-model',
    title: '添加自定义模型（OpenCode）',
    width: 1000,
    height: 900,
    status: 'shipped',
    fixtures: { api },
    render: () => (
      <BackendModelEditorDialog
        open
        backend="opencode"
        model={null}
        takenIds={opencodeListed}
        effortSuggestions={['low', 'medium', 'high']}
        onCancel={() => {}}
        onCommit={() => {}}
      />
    ),
  },
  { name: 'custom-model-exact', title: '添加自定义模型 · 目录里有这个 ID：自动填好，写明参数来自哪里', ...customFrame, render: custom(exactId) },
  { name: 'custom-model-closest', title: '添加自定义模型 · 目录里没有这个 ID：按最接近的模型填，可以更换', ...customFrame, render: custom(variantId) },
  { name: 'custom-model-unmatched', title: '添加自定义模型 · 目录里找不到：直说，并给常见值', ...customFrame, render: custom(unknownId) },
  {
    name: 'custom-model-offer',
    title: '添加自定义模型 · 改了 ID：参数不动，多一个「按新 ID 更新参数」',
    ...customFrame,
    click: [{ fill: idField, text: 'claude-fable-5' }],
    render: custom(exactId),
  },
  {
    name: 'custom-model-change',
    title: '添加自定义模型 · 更换：搜索模型目录，只套用参数',
    ...customFrame,
    click: [change, { fill: 'role=textbox[name="{t:settings.models.gateway.modelEditor.source.search}"]', text: 'fable' }],
    render: custom(variantId),
  },
  {
    name: 'custom-model-change-phone',
    title: '添加自定义模型 · 更换 · 手机',
    ...customFrame,
    width: 390,
    height: 844,
    click: [change, { fill: 'role=textbox[name="{t:settings.models.gateway.modelEditor.source.search}"]', text: 'fable' }],
    render: custom(variantId),
  },
]);

export default designFrames;
