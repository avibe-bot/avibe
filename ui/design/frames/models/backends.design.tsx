// Settings · Backends: the shipped list of Agent backends, each with its lifecycle chip
// (ready, update available, disabled, CLI missing) and enable switch.
import { SettingsBackendsPage } from '@/components/settings/SettingsBackendsPage';

import { frames } from '../../frame';
import { settingsPage } from '../settings/fixtures';
import { backendsFixtures } from './backends.fixtures';

const page = settingsPage('backends', <SettingsBackendsPage />);
const route = '/settings/backends';

// Codex switched off, and OpenCode's CLI gone from PATH after a reinstall.
const attention = backendsFixtures({
  enabled: { codex: false },
  runtimes: { codex: { process_status: 'stopped' }, opencode: { installed: false, status: 'missing', resolved_path: null, current_version: null, reason: 'not_found', process_status: 'stopped' } },
});

const designFrames = frames([
  {
    name: 'list',
    title: '设置 · 后端',
    width: 1440,
    height: 760,
    status: 'shipped',
    route,
    fixtures: backendsFixtures(),
    render: page,
  },
  {
    name: 'attention',
    title: '设置 · 后端 · 已停用、CLI 未找到',
    width: 1440,
    height: 760,
    status: 'shipped',
    route,
    fixtures: attention,
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 后端 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: backendsFixtures(),
    render: page,
  },
]);

export default designFrames;
