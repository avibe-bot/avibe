// Settings · Models (the Model Hub): the shipped page on its 供应商与路由 tab, rendered from
// `/api/models/*` fixtures. Providers on the left, each backend's model routes on the right.
import { SettingsModelsPage } from '@/components/settings/models/SettingsModelsPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import {
  chatgptPro,
  claudeMax,
  demoAgents,
  demoSources,
  failed,
  hubApi,
  runtime,
  type AgentSpec,
} from './fixtures';

// Inside the shipped Settings layout, at the route App.tsx gives the Models page.
const page = settingsPage('models', <SettingsModelsPage />);
const route = '/settings/models';

// Claude Max ran out of its 5-hour quota: Claude Code's routes moved to the relay until it returns.
const claudeCooling = { ...claudeMax, state: { status: 'cooldown' as const, retry_at: '2026-11-01T08:42:00Z', detail_key: 'models.source.cooldown.quota_exhausted' as const } };
const takeoverAgents: AgentSpec[] = demoAgents.map((spec) => spec.backend !== 'claude' ? spec : {
  ...spec,
  routes: Object.fromEntries(Object.entries(spec.routes ?? {}).map(([id, chain]) => [id, { ...chain, health: { [claudeMax.id]: 'cooldown' } }])),
});

// The ChatGPT login expired: Codex and Vibey fall back to the relay, and the row asks to sign in again.
const chatgptExpired = { ...chatgptPro, state: { status: 'needs_action' as const, retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' as const } };
const expiredAgents: AgentSpec[] = demoAgents.map((spec) => ({
  ...spec,
  routes: Object.fromEntries(Object.entries(spec.routes ?? {}).map(([id, chain]) => [id, { ...chain, health: { [chatgptPro.id]: 'needs_action' } }])),
}));

// A fresh switch to the gateway: no provider yet, so every model waits for one.
const unsuppliedAgents: AgentSpec[] = [
  { backend: 'claude', routes: { 'claude-opus-5': { hops: [] }, 'claude-sonnet-5': { hops: [] }, 'claude-haiku-5': { hops: [] } } },
  { backend: 'codex', routes: { 'gpt-6-astra': { hops: [] }, 'gpt-5.6-sol': { hops: [] }, 'gpt-5.5': { hops: [] } } },
];

const directAgents: AgentSpec[] = [{ backend: 'claude', mode: 'direct' }, { backend: 'codex', mode: 'direct' }, { backend: 'opencode', mode: 'direct' }];

const designFrames = frames([
  {
    name: 'overview',
    title: '模型网关 · 供应商与路由',
    width: 1440,
    height: 1720,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ agents: demoAgents })),
    render: page,
  },
  {
    name: 'add-subscription-menu',
    title: '模型网关 · 添加订阅 · 选择服务商',
    width: 1440,
    height: 1200,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ agents: demoAgents })),
    click: ['role=button[name="添加订阅"]'],
    render: page,
  },
  {
    name: 'add-provider-menu',
    title: '模型网关 · 添加供应商 · 选择方式',
    width: 1440,
    height: 1200,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ agents: demoAgents })),
    click: ['role=button[name="添加供应商"]'],
    render: page,
  },
  {
    name: 'overview-phone',
    title: '模型网关 · 供应商与路由 · 手机',
    width: 390,
    height: 1800,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ agents: demoAgents })),
    render: page,
  },
  {
    name: 'origin-help',
    title: '模型网关 · 路由标签说明（悬停）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ agents: demoAgents })),
    hover: '[data-route-backend="claude"][data-route-model="claude-haiku-5"] span.relative > button',
    render: page,
  },
  {
    name: 'takeover',
    title: '模型网关 · 已自动切换（Claude Max 额度用完）',
    width: 1440,
    height: 1100,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ sources: demoSources.map((s) => (s.id === claudeMax.id ? claudeCooling : s)), agents: takeoverAgents })),
    render: page,
  },
  {
    name: 'needs-action',
    title: '模型网关 · 供应商需要处理（ChatGPT 登录过期）',
    width: 1440,
    height: 1100,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ sources: demoSources.map((s) => (s.id === chatgptPro.id ? chatgptExpired : s)), agents: expiredAgents })),
    render: page,
  },
  {
    name: 'no-providers',
    title: '模型网关 · 已切到网关、还没有供应商',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ sources: [], agents: unsuppliedAgents })),
    render: page,
  },
  {
    name: 'direct',
    title: '模型网关 · 全部直连（升级后第一屏）',
    width: 1440,
    height: 760,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(
      hubApi({
        sources: [],
        agents: directAgents,
        migration: [{ id: 'mig_claude', backend: 'claude', kind: 'oauth_native', masked_detail: 'Claude 本机登录', proposed_action: 'import', selected: true, notes_key: null, vendor: 'anthropic', display_name: 'Anthropic' }],
      }),
    ),
    render: page,
  },
  {
    name: 'runtime-off',
    title: '模型网关 · 暂不可用（运行时已停止）',
    width: 1440,
    height: 600,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ agents: demoAgents, runtime: runtime('down', false) })),
    render: page,
  },
  {
    name: 'runtime-not-installed',
    title: '模型网关 · 组件未安装',
    width: 1440,
    height: 600,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(hubApi({ agents: demoAgents, runtime: runtime('not_installed', false) })),
    render: page,
  },
  {
    name: 'read-failed',
    title: '模型网关 · 读取失败',
    width: 1440,
    height: 760,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ ...hubApi({ agents: demoAgents }), '/api/models/sources': failed(), '/api/models/agents': failed() }),
    render: page,
  },
]);

export default designFrames;
