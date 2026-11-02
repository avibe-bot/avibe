// Settings · Computer Use: let an Agent see and operate this Mac, with the macOS permissions it needs.
import { SettingsComputerUsePage } from '@/components/settings/SettingsComputerUsePage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('computer-use', <SettingsComputerUsePage />);
const route = '/settings/computer-use';
const status = (value: Record<string, unknown>) => settingsFixtures({ '/api/desktop/computer-use/status': value });

const designFrames = frames([
  {
    name: 'ready',
    title: '设置 · 电脑操作 · 已就绪',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: status({ supported: true, status: 'ready', reason: null }),
    render: page,
  },
  {
    name: 'needs-permission',
    title: '设置 · 电脑操作 · 需要辅助功能权限',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: status({ supported: true, status: 'needs_permission', reason: 'accessibility' }),
    render: page,
  },
  {
    name: 'browser',
    title: '设置 · 电脑操作 · 在浏览器里打开',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: status({ supported: false }),
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 电脑操作 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: status({ supported: true, status: 'needs_permission', reason: 'accessibility' }),
    render: page,
  },
]);

export default designFrames;
