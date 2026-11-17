// Settings · Models (Model Hub): under each model on a gateway card, one quiet line names who uses
// it — a Bot by its own name, any other Agent by its Agent name — and nothing when nobody does.
// Four or more read as the first two and the total. Problems stay in the folded 「需关注」 footer.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames, type FrameSpec } from '../../frame';
import { bot } from '../../shell/fixtures';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { hubApi, relayClaude, type AgentSpec } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';

// The main Bot 「歪比」 and the Agent 「vibey」 run Opus; two more Bots and three Agents run
// Sonnet; nobody runs Haiku.
const weibi = bot('main', '歪比', "The user's main Bot and first point of contact.", { kind: 'mascot' });
const cso = bot('cso', '首席安全官', '依赖与权限的安全审计，每周汇报', { kind: 'emoji', emoji: '🛡️', hue: 200 });
const pm = bot('pm', '产品经理', '需求池整理与优先级', { kind: 'emoji', emoji: '🧭', hue: 42 });
const uses = (model: string, ...names: string[]) => names.map((name) => ({ name, effective_model_id: model, supply_status: 'ok' as const, route_reason: null }));
const vibey: AgentSpec = {
  backend: 'vibey',
  cliPresent: false,
  order: [relayClaude],
  routes: {
    'claude-opus-5': { hops: [[relayClaude, 'claude-opus-5']] },
    'claude-sonnet-5': { hops: [[relayClaude, 'claude-sonnet-5']] },
    'claude-haiku-5': { hops: [[relayClaude, 'claude-haiku-5']] },
  },
  namedAgents: [
    ...uses('claude-opus-5', 'main', 'vibey'),
    ...uses('claude-sonnet-5', 'cso', 'reviewer', 'pm', 'slack-triage', 'release-notes'),
  ],
};
// The hub's reads answer `/api/bots` with no Bots; these Bots are served from `bots` instead.
const { '/api/bots': _noBots, ...api } = hubApi({ sources: [relayClaude], agents: [vibey] });
const fixtures = settingsFixtures(api, { bots: [weibi, cso, pm] });

const view = (name: string, title: string, width: number, height: number): FrameSpec => ({
  name, title, width, height, themes: ['light'], status: 'shipped', route, fixtures, render: page,
});

const designFrames = frames([
  view('desktop', '模型网关 · 每个模型下写谁在用', 1440, 900),
  view('phone', '模型网关 · 每个模型下写谁在用 · 手机', 390, 1300),
]);

export default designFrames;
