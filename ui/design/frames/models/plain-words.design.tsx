// Settings · Models in plain words. A provider just added whose model list came back with the key
// reads as usable and names who uses it, instead of 「已保存」 until the first chat. Provider cards
// name the kind only (the API format is in the provider's details), and a model no provider lists
// reads 「原样转发」 rather than 「透传」.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames, type FrameSpec } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { demoAgents, demoSources, hubApi, relayClaude, type AgentSpec } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';
const themes: FrameSpec['themes'] = ['light'];

// Just added: saved, its model list fetched with the key, no chat yet. Vibey picked a model on it.
const justAdded = { ...relayClaude, verification_pending: 'vp_design', adopted_by: [{ backend: 'vibey' as const, menu_model: 'claude-opus-5' }] };
const firstAgents: AgentSpec[] = [
  { backend: 'vibey', cliPresent: false, order: [justAdded], routes: { 'claude-opus-5': { hops: [[justAdded, 'claude-opus-5']] } } },
  { backend: 'claude', cliPresent: false },
  { backend: 'codex', cliPresent: false },
  { backend: 'opencode', cliPresent: false },
];

const designFrames = frames([
  { name: 'just-added', title: '模型网关 · 刚加好的供应商（已拉到模型列表，还没对话过）', width: 1440, height: 760, themes, status: 'shipped', route, fixtures: settingsFixtures(hubApi({ sources: [justAdded], agents: firstAgents })), render: page },
  { name: 'overview', title: '模型网关 · 卡片不写 API 格式；没列出的模型「原样转发」', width: 1440, height: 2200, themes, status: 'shipped', route, fixtures: settingsFixtures(hubApi({ sources: demoSources, agents: demoAgents })), render: page },
]);

export default designFrames;
