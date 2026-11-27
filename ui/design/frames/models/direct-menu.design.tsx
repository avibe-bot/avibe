// Settings · Models with Codex run directly (not through the gateway): its card lists the Codex menu
// the Session model picker, `vibe agent models` and the IM routing form offer; whoever may add models removes a
// model there or brings one back.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { demoAgents, hubApi, ok, type AgentSpec } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';

const agents: AgentSpec[] = demoAgents.map((spec) => spec.backend === 'codex' ? { backend: 'codex', mode: 'direct' } : spec);
const efforts = [{ value: '__default__', label: '(Default)' }, ...['low', 'medium', 'high', 'xhigh'].map((e) => ({ value: e, label: e }))];
const menu = ['gpt-6.2-sol', 'gpt-6.2-astra', 'gpt-6.1-sol', 'gpt-6-astra', 'gpt-5.6-terra'];
const labels: Record<string, string> = { 'gpt-6.2-sol': 'GPT-6.2-Sol', 'gpt-6.2-astra': 'GPT-6.2-Astra', 'gpt-6.1-sol': 'GPT-6.1-Sol', 'gpt-6-astra': 'GPT-6-Astra', 'gpt-5.6-terra': 'GPT-5.6-Terra' };

const direct = (removed: string[]) => settingsFixtures({
  ...hubApi({ agents }),
  '/api/models/agents/codex/models': ok({ agent: { backend: 'codex', mode: 'direct', catalog_models: [] } }),
  '/api/codex/models': {
    ok: true,
    backend: 'codex',
    models: menu,
    model_labels: labels,
    reasoning_options: Object.fromEntries(['', ...menu].map((id) => [id, efforts])),
  },
  '/api/backend/codex/models/visibility': ok({ models: removed.map((id) => ({ id, label: id })) }),
});

const designFrames = frames([
  { name: 'list', title: 'Codex 直连 · 卡片列出它的模型菜单', width: 1440, height: 1900, themes: ['light'], status: 'shipped', route, fixtures: direct([]), render: page },
  { name: 'removed', title: 'Codex 直连 · 已移除一个模型，可恢复', width: 1440, height: 1900, themes: ['light'], status: 'shipped', route, fixtures: direct(['gpt-5.5']), click: ['[data-direct-removed-toggle]'], render: page },
  { name: 'phone', title: 'Codex 直连 · 手机', width: 390, height: 2800, themes: ['light'], status: 'shipped', route, fixtures: direct(['gpt-5.5']), render: page },
]);

export default designFrames;
