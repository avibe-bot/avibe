// Proposal · adding a model where the models are listed.
//
// Today a backend's models are added through 管理模型 → 添加模型 → search, and a search that finds
// nothing ends there in setup. Here every model list on Settings · Models ends in 添加模型, and the
// dialog it opens searches the providers and the model catalog together and offers what was typed as a
// custom model when nothing else matches.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames } from '../../frame';
import { AddModelDialog, ModelListAddRow, type AddModelRow } from '../../kit/model-add';
import { IntoPage } from '../../shell/IntoPage';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { demoAgents, hubApi } from './fixtures';

const page = settingsPage('models', (
  <>
    <SettingsModelsPage />
    {/* The model list of each backend the gateway serves. */}
    <IntoPage selector="[data-agent-backend] > div.space-y-2"><ModelListAddRow /></IntoPage>
  </>
));

const relay = 'ai-relay-claude';
const builtin: AddModelRow[] = [
  { id: 'claude-opus-5-5', name: 'Claude Opus 5.5' },
  { id: 'claude-haiku-5-5', name: 'Claude Haiku 5.5' },
];
const fromProviders: AddModelRow[] = [
  { id: 'claude-fable-5-1', name: 'Claude Fable 5.1', providers: [relay] },
  { id: 'claude-opus-5', name: 'Claude Opus 5', providers: [relay] },
  { id: 'claude-sonnet-5', name: 'Claude Sonnet 5', providers: [relay] },
  { id: 'gpt-6-astra', name: 'GPT-6 Astra', providers: ['ai-relay-gpt'] },
  { id: 'gpt-6.1-terra', name: 'GPT-6.1 Terra', providers: ['ai-relay-gpt'] },
  { id: 'deepseek-v4', providers: ['DeepSeek'] },
];
const picking = fromProviders.map((row) => (row.id === 'claude-fable-5-1' || row.id === 'gpt-6.1-terra' ? { ...row, picked: true } : row));

// 「Fable 5.2」: the provider lists 5 and 5.1, the catalog knows 5.2.
const fableListed: AddModelRow[] = [{ id: 'claude-fable-5', name: 'Claude Fable 5', providers: [relay] }];
const fableCatalog: AddModelRow[] = [{ id: 'claude-fable-5-2', name: 'Claude Fable 5.2', picked: true }];

const designFrames = frames([
  {
    name: 'routes-entry',
    title: '提案 · 模型网关 · 每个后端的模型列表以「添加模型」结尾',
    width: 1440,
    height: 1900,
    status: 'proposed',
    route: '/settings/models',
    fixtures: settingsFixtures(hubApi({ agents: demoAgents })),
    render: page,
  },
  {
    name: 'dialog',
    title: '提案 · 添加模型 · 打开时',
    width: 1000,
    height: 820,
    status: 'proposed',
    fixtures: { api: hubApi({ agents: demoAgents }) },
    render: () => <AddModelDialog backend="Vibey" builtin={builtin} providers={picking} />,
  },
  {
    name: 'search-catalog',
    title: '提案 · 添加模型 · 供应商没列出、模型目录里有',
    width: 1000,
    height: 820,
    status: 'proposed',
    fixtures: { api: hubApi({ agents: demoAgents }) },
    render: () => <AddModelDialog backend="Vibey" query="Fable 5.2" catalog={fableCatalog} listed={fableListed} />,
  },
  {
    name: 'search-none',
    title: '提案 · 添加模型 · 哪里都搜不到',
    width: 1000,
    height: 820,
    status: 'proposed',
    fixtures: { api: hubApi({ agents: demoAgents }) },
    render: () => <AddModelDialog backend="Vibey" query="my-finetune-v3" />,
  },
  {
    name: 'setup-choose',
    title: '提案 · 设置向导 · 为 Vibey 选模型时搜索',
    width: 1000,
    height: 820,
    status: 'proposed',
    fixtures: { api: hubApi({ agents: demoAgents }) },
    render: () => <AddModelDialog backend="Vibey" choose="one" query="Fable 5.2" catalog={fableCatalog} providers={fableListed} />,
  },
  {
    name: 'search-catalog-phone',
    title: '提案 · 添加模型 · 手机',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: { api: hubApi({ agents: demoAgents }) },
    render: () => <AddModelDialog backend="Vibey" query="Fable 5.2" catalog={fableCatalog} listed={fableListed} />,
  },
]);

export default designFrames;
