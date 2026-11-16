// Settings · Models (Model Hub) with a backend switched off in Settings · Backends. An installed
// backend that is off is drawn as one folded row after the backends that are on: no models, no
// actions, no wires, and providers do not name it as a user. A backend that is not installed has
// no row, as before.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames, type FrameSpec } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { chatgptPro, claudeMax, demoAgents, demoSources, hubApi, relayClaude, type AgentSpec } from './fixtures';

const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';
const themes: FrameSpec['themes'] = ['light'];

type Backend = AgentSpec['backend'];
const off = (...backends: Backend[]) => (spec: AgentSpec): AgentSpec => backends.includes(spec.backend) ? { ...spec, enabled: false } : spec;
const missing = (...backends: Backend[]) => (spec: AgentSpec): AgentSpec => backends.includes(spec.backend) ? { ...spec, cliPresent: false } : spec;

// The owner's report: Vibey on a relay; Codex installed but switched off; Claude Code and OpenCode
// switched off and not installed. The relay still lists Codex among its users (its saved routes),
// and the page names only Vibey.
const ownersRelay = { ...relayClaude, adopted_by: [{ backend: 'codex' as const, menu_model: 'claude-opus-5' }, { backend: 'vibey' as const, menu_model: 'claude-opus-5' }] };
const ownersCase: AgentSpec[] = [
  { ...demoAgents.find((spec) => spec.backend === 'vibey')!, order: [ownersRelay], routes: { 'claude-opus-5': { hops: [[ownersRelay, 'claude-opus-5']] }, 'claude-sonnet-5': { hops: [[ownersRelay, 'claude-sonnet-5']] } } },
  { ...demoAgents.find((spec) => spec.backend === 'codex')!, enabled: false },
  { backend: 'claude', cliPresent: false, enabled: false },
  { backend: 'opencode', cliPresent: false, enabled: false },
];

// A sign-in expired on a provider whose backend is also switched off: the provider row asks to sign
// in again and names only the backends that are on.
const expire = (id: string) => demoSources.map((s) => s.id === id ? { ...s, state: { status: 'needs_action' as const, retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' as const } } : s);
const expiredRoutes = (id: string) => (spec: AgentSpec): AgentSpec => ({
  ...spec,
  routes: Object.fromEntries(Object.entries(spec.routes ?? {}).map(([model, chain]) => [model, { ...chain, health: { [id]: 'needs_action' } }])),
});

const directAll: AgentSpec[] = [{ backend: 'claude', mode: 'direct' }, { backend: 'codex', mode: 'direct' }, { backend: 'opencode', mode: 'direct' }];

const gateway = (name: string, title: string, agents: AgentSpec[], { width = 1440, height = 1720, sources = demoSources } = {}): FrameSpec => ({
  name, title, width, height, themes, status: 'proposed', route,
  fixtures: settingsFixtures(hubApi({ sources, agents })),
  render: page,
});

const designFrames = frames([
  gateway('owners-case', '模型网关 · Codex 已停用（Claude Code、OpenCode 未安装）', ownersCase, { height: 900, sources: [ownersRelay] }),
  gateway('owners-case-phone', '模型网关 · Codex 已停用 · 手机', ownersCase, { width: 390, height: 1400, sources: [ownersRelay] }),
  gateway('claude-off', '模型网关 · 只停用了 Claude Code', demoAgents.map(off('claude'))),
  gateway('codex-off', '模型网关 · 只停用了 Codex', demoAgents.map(off('codex'))),
  gateway('opencode-off', '模型网关 · 只停用了 OpenCode', demoAgents.map(off('opencode')), { height: 1900 }),
  gateway('claude-off-codex-missing', '模型网关 · Claude Code 已停用，Codex 未安装（没有行）', demoAgents.map(off('claude')).map(missing('codex')), { height: 1300 }),
  gateway('all-off', '模型网关 · 三个本机后端都已停用', demoAgents.map(off('claude', 'codex', 'opencode')), { height: 1100 }),
  gateway('all-off-phone', '模型网关 · 三个本机后端都已停用 · 手机', demoAgents.map(off('claude', 'codex', 'opencode')), { width: 390, height: 1500 }),
  gateway('signed-out-codex-off', '模型网关 · ChatGPT 登录过期，Codex 已停用', demoAgents.map(off('codex')).map(expiredRoutes(chatgptPro.id)), { sources: expire(chatgptPro.id) }),
  gateway('signed-out-claude-off', '模型网关 · Claude 登录过期，Claude Code 已停用', demoAgents.map(off('claude')).map(expiredRoutes(claudeMax.id)), { sources: expire(claudeMax.id) }),
  gateway('direct-codex-off', '模型网关 · Agent 管理首页，Codex 已停用', directAll.map(off('codex')), { height: 760, sources: [] }),
  gateway('direct-all-off', '模型网关 · Agent 管理首页，三个都已停用', directAll.map(off('claude', 'codex', 'opencode')), { height: 760, sources: [] }),
  gateway('direct-off-phone', '模型网关 · Agent 管理首页，Claude Code 已停用 · 手机', directAll.map(off('claude')).map(missing('opencode')), { width: 390, height: 1100, sources: [] }),
]);

export default designFrames;
