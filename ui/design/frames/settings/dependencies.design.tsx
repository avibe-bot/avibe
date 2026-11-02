// Settings · Dependencies: the local runtimes Avibe installs and keeps current, one row each.
import type { DependencyItem } from '@/context/ApiContext';
import { SettingsDependenciesPage } from '@/components/settings/SettingsDependenciesPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('dependencies', <SettingsDependenciesPage />);
const route = '/settings/dependencies';

const dep = (id: string, version: string | null, extra: Partial<DependencyItem> = {}): DependencyItem => ({
  id,
  kind: id === 'node' ? 'node' : id === 'show-runtime' ? 'runtime' : 'tool',
  required: true,
  installed: version !== null,
  version,
  latest_version: version,
  has_update: false,
  status: 'ready',
  readiness: 'ready',
  action_class: 'repairable',
  reason: null,
  ...extra,
});

const current = [
  dep('askill', '0.1.13'),
  dep('avault', '0.6.2', { latest_version: '0.7.0', has_update: true }),
  dep('show-runtime', '1.4.0'),
  dep('node', '22.11.0', { action_class: 'none' }),
  dep('model-hub-engine', '0.9.3'),
  dep('tmux', '3.5a', { required: false }),
];

const broken = [
  dep('askill', '0.1.13'),
  dep('avault', '0.7.0'),
  dep('show-runtime', null, { status: 'missing', readiness: 'not_ready' }),
  dep('node', null, { status: 'missing', readiness: 'not_ready', action_class: 'operator_only' }),
  dep('model-hub-engine', '0.9.1', { status: 'error', readiness: 'not_ready', reason: 'engine_start_failed' }),
  dep('tmux', '3.5a', { required: false }),
];

const designFrames = frames([
  {
    name: 'default',
    title: '设置 · 依赖 · 一项可更新',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/dependencies': { ok: true, deps: current } }),
    render: page,
  },
  {
    name: 'needs-repair',
    title: '设置 · 依赖 · 缺失与出错',
    width: 1440,
    height: 1000,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/dependencies': { ok: true, deps: broken } }),
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 依赖 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/dependencies': { ok: true, deps: current } }),
    render: page,
  },
]);

export default designFrames;
