// A backend's model menu (管理模型): the shipped BackendModelCatalogDialog, its picker for adding
// models providers supply, and its editor for one model.
import { BackendModelCatalogDialog } from '@/components/settings/models/BackendModelCatalogDialog';
import { BackendModelEditorDialog } from '@/components/settings/models/BackendModelEditorDialog';
import { BackendModelPickerDialog } from '@/components/settings/models/BackendModelPickerDialog';
import type { AgentBackend, Source } from '@/components/settings/models/types';

import { frames } from '../../frame';
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
const menuAgents: AgentSpec[] = demoAgents.map((spec) => spec.backend !== 'claude' ? spec : {
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

const api = { ...hubApi({ agents: menuAgents }), '/api/models/agents/opencode/models/candidates': opencodeCandidates };

const editing = backendModel('claude-opus-4-8', { origin: 'provider', reasoning_efforts: claudeEfforts, supports_reasoning: true, supports_tools: true, context_window: 1_000_000, max_output_tokens: 128_000 });

const opencodeListed = new Set(['deepseek-v4', 'deepseek-v4-reasoner', 'claude-sonnet-5', 'gpt-5.6-sol']);
const claudeTaken = new Set(['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5']);

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
    render: () => (
      <BackendModelPickerDialog
        open
        backend="opencode"
        listedIds={opencodeListed}
        onCancel={() => {}}
        onAdd={() => {}}
        onCustom={() => {}}
      />
    ),
  },
  {
    name: 'edit-model',
    title: '编辑模型 · claude-opus-4-8',
    width: 1000,
    height: 900,
    status: 'shipped',
    fixtures: { api },
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
]);

export default designFrames;
