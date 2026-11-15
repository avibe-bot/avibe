// Settings · Computer Use: let an Agent see and operate this Mac, with the macOS permissions it needs,
// or a Windows PC in Preview, with the switch alone. The host comes from the server's `host_platform`.
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
    name: 'windows-off',
    title: '设置 · 电脑操作 · Windows 预览 · 未开启',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(
      { '/api/desktop/computer-use/status': { supported: true, host_platform: 'windows', status: 'off', reason: 'never_enabled' } },
      { desktopShell: true },
    ),
    render: page,
  },
  {
    name: 'windows-browser',
    title: '设置 · 电脑操作 · Windows 预览 · 在浏览器里查看（会话已锁定）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: status({ supported: true, host_platform: 'windows', status: 'unavailable', reason: 'session_locked' }),
    render: page,
  },
  {
    name: 'windows-unsupported',
    title: '设置 · 电脑操作 · Windows 预览 · 桌面应用未运行',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: status({ supported: false, host_platform: 'windows', status: 'unavailable', reason: 'unsupported_host' }),
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
