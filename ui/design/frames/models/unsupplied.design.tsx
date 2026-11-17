// A relay that lists only Anthropic models, with Codex's GPT models in its list. No provider
// supplies them, and the page says so on each row; Codex's own status says the same, while
// Vibey, whose listed model is routed, stays 路由可用. A model's route dialog says how to supply
// it by hand.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames, type FrameSpec } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { hubApi, relayClaude, type AgentSpec } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';
const themes: FrameSpec['themes'] = ['light'];
const relay = { ...relayClaude, adopted_by: [{ backend: 'vibey' as const, menu_model: 'claude-opus-5' }] };

const agents: AgentSpec[] = [
  { backend: 'vibey', cliPresent: false, order: [relay], routes: { 'claude-opus-5': { hops: [[relay, 'claude-opus-5']] }, 'gpt-6-astra': { hops: [] } } },
  { backend: 'codex', order: [relay], routes: { 'gpt-6-astra': { hops: [] }, 'gpt-5.6-sol': { hops: [] }, 'gpt-5.5': { hops: [] } } },
  { backend: 'claude', cliPresent: false },
  { backend: 'opencode', cliPresent: false },
];

const designFrames = frames([
  { name: 'overview', title: '模型网关 · 供应商没列出的模型「没有供应商提供」', width: 1440, height: 1250, themes, status: 'shipped', route, fixtures: settingsFixtures(hubApi({ sources: [relay], agents })), render: page },
]);

export default designFrames;
