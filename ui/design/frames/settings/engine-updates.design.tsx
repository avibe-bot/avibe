// Settings · Dependencies · the Model Hub engine's own updates (docs/plans/model-hub-engine-channel.md,
// section 5). The engine's row keeps its line and gains one strip: one sentence, the automatic-update
// switch, one button. Each frame is the shipped page with the engine's row in one state, as
// `GET /api/dependencies` reports it; the strip is `kit/engine-updates` until it ships.
import type { DependencyItem } from '@/context/ApiContext';
import { SettingsDependenciesPage } from '@/components/settings/SettingsDependenciesPage';

import { frames, type FrameSpec } from '../../frame';
import { EngineUpdateStrip } from '../../kit/engine-updates';
import { DESIGN_NOW } from '../../shell/clock';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('dependencies', <SettingsDependenciesPage engineUpdates={EngineUpdateStrip} />);
const route = '/settings/dependencies';
const ago = (minutes: number) => new Date(DESIGN_NOW - minutes * 60_000).toISOString();

const RUNNING = 'model-hub-engine-v8.0.63-2';
const NEXT = 'model-hub-engine-v8.0.70-1';

const dep = (id: string, version: string, extra: Partial<DependencyItem> = {}): DependencyItem => ({
  id,
  kind: id === 'node' ? 'node' : id === 'show-runtime' ? 'runtime' : 'tool',
  required: true,
  installed: true,
  version,
  latest_version: version,
  has_update: false,
  status: 'ready',
  readiness: 'ready',
  action_class: 'repairable',
  reason: null,
  ...extra,
});

/** The page with the engine's row as the channel reports it; the other rows are ready. */
const deps = (engine: Partial<DependencyItem>) => [
  dep('askill', '0.1.13'),
  dep('avault', '0.7.0'),
  dep('show-runtime', '1.4.0'),
  dep('node', '22.11.0', { action_class: 'none' }),
  dep('model-hub-engine', '8.0.63', {
    active_release: RUNNING,
    default_release: 'model-hub-engine-v8.0.63-1',
    available_release: null,
    min_avibe_version: null,
    auto_update: true,
    update_state: 'up_to_date',
    update_reason: 'up_to_date',
    last_checked_at: ago(5),
    last_result: null,
    operation_id: null,
    ...engine,
  }),
  dep('tmux', '3.5a', { required: false }),
];

// "Pinned" stops being true once the engine updates by itself, and the line named the upstream project.
const copy = {
  'settings.dependencies.items.model-hub-engine.detail': {
    zh: '在本机运行模型网关的引擎。',
    en: 'The local engine that runs Model Hub.',
  },
};

const state = (name: string, title: string, engine: Partial<DependencyItem>, channel = true): FrameSpec[] => {
  const fixtures = settingsFixtures({ '/api/dependencies': { ok: true, deps: deps(engine) } }, channel ? { copy } : {});
  return [
    { name, title: `设置 · 依赖 · 引擎更新 · ${title}`, width: 1440, height: 960, status: 'proposed', route, fixtures, render: page },
    { name: `${name}-phone`, title: `设置 · 依赖 · 引擎更新 · ${title} · 手机`, width: 390, height: 1400, status: 'proposed', route, fixtures, render: page },
  ];
};

const designFrames = frames([
  ...state('up-to-date', '已是最新', {}),
  ...state('available', '有新版本', { update_state: 'available', update_reason: null, available_release: NEXT, min_avibe_version: '3.2.6' }),
  ...state('updating', '更新中', { update_state: 'updating', update_reason: null, available_release: NEXT, operation_id: 'engine-7f3a' }),
  ...state('waiting-for-idle', '等空闲后更新', { update_state: 'deferred', update_reason: 'waiting_for_idle', available_release: NEXT }),
  // `rolled_back` lasts until the next check; the recorded result keeps the notice after it.
  ...state('undone', '更新被撤销，已换回上一个版本', { last_result: 'rolled_back', last_checked_at: ago(2) }),
  ...state('needs-avibe', '需要更新的 Avibe', { update_state: 'requires_avibe_update', update_reason: 'requires_avibe_update', min_avibe_version: '3.2.7' }),
  ...state('cannot-check', '离线或读不到发布列表', { update_state: 'unavailable', update_reason: 'fetch_failed', last_checked_at: ago(130) }),
  ...state('automatic-off', '关闭自动更新', { auto_update: false, update_state: 'available', update_reason: null, available_release: NEXT, min_avibe_version: '3.2.6' }),
  // Withdrawn with no usable fallback: Model Hub stops, the rest of Avibe goes on. Nothing here repairs it.
  ...state('stopped', '引擎被撤回且没有可用版本，模型网关停止', {
    status: 'error', action_class: 'operator_only', reason: 'model_hub_engine_install_inspection_failed',
    active_release: null, update_state: 'blocked', update_reason: 'engine_withdrawn', last_checked_at: ago(1),
  }),
  // This build ships no channel: the row is the one shipped today, with nothing added.
  ...state('channel-off', '本版本未启用更新通道（与今天相同）', {
    active_release: null, default_release: 'model-hub-engine-v8.0.63-1', update_state: 'uncommissioned', update_reason: 'uncommissioned', last_checked_at: null,
  }, false),
]);

export default designFrames;
