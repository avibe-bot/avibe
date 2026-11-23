// The Models page before the first model provider, and the API key form a newcomer opens next.
// A fresh install: Vibey has no model yet, the CLI backends are not running, and no provider has
// been added. The page offers one way forward, the Providers card's add buttons: no 「添加模型」
// in the routes column and no attention count for Agents that cannot have a model yet. The API key
// form asks only for what it cannot work out: a preset fixes the format, a custom address is
// detected, and the optional name waits under 「更多设置」.
import { AddApiKeyDialog } from '@/components/settings/models/AddApiKeyDialog';
import { createSourceCollectionReadAuthority } from '@/components/settings/models/collectionReadAuthority';
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames, type FrameSpec } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { baseApi, hubApi, type AgentSpec } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';
const themes: FrameSpec['themes'] = ['light'];

// Fresh install: Vibey's catalog is empty and both of its Agents (the main Bot and vibey) have no
// model; the CLI backends are not installed here, so only Vibey is drawn.
const fresh: AgentSpec[] = [
  {
    backend: 'vibey',
    cliPresent: false,
    catalog: [],
    namedAgents: [
      { name: 'main', effective_model_id: null, supply_status: null },
      { name: 'vibey', effective_model_id: null, supply_status: null },
    ],
  },
  { backend: 'claude', cliPresent: false },
  { backend: 'codex', cliPresent: false },
  { backend: 'opencode', cliPresent: false },
];

const sourceReads = createSourceCollectionReadAuthority();
const addApiKey = () => <AddApiKeyDialog open sourceReads={sourceReads} onClose={() => {}} onAdded={() => {}} />;

const designFrames = frames([
  { name: 'fresh', title: '模型网关 · 新装、还没有供应商', width: 1440, height: 760, themes, status: 'shipped', route, fixtures: settingsFixtures(hubApi({ sources: [], agents: fresh })), render: page },
  { name: 'fresh-phone', title: '模型网关 · 新装、还没有供应商 · 手机', width: 390, height: 1100, themes, status: 'shipped', route, fixtures: settingsFixtures(hubApi({ sources: [], agents: fresh })), render: page },
  { name: 'api-key-custom', title: '添加 API Key · 自己填地址', width: 1000, height: 760, themes, status: 'shipped', fixtures: { api: baseApi }, render: addApiKey },
  {
    name: 'api-key-preset',
    title: '添加 API Key · 选了 DeepSeek',
    width: 1000,
    height: 760,
    themes,
    status: 'shipped',
    fixtures: { api: baseApi },
    click: ['role=combobox', 'role=option[name=/DeepSeek/]'],
    render: addApiKey,
  },
]);

export default designFrames;
