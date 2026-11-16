// Settings · Computer Use: let an Agent see and operate this Mac, with the macOS permissions it needs,
// or a Windows PC in Preview, with the switch alone. The host comes from the server's `host_platform`.
import { SettingsComputerUsePage } from '@/components/settings/SettingsComputerUsePage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('computer-use', <SettingsComputerUsePage />);
const route = '/settings/computer-use';
const status = (value: Record<string, unknown>) => settingsFixtures({ '/api/desktop/computer-use/status': value });
/** A Windows host seen from its own desktop app, whose shell turns Computer Use on (and off). */
const windowsShell = (value: Record<string, unknown>, actions: 'on' | 'on-off' = 'on-off') => settingsFixtures(
  { '/api/desktop/computer-use/status': { supported: true, host_platform: 'windows', ...value } },
  { desktopShell: true, desktopComputerUse: actions },
);

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
    title: '设置 · 电脑操作 · Windows 预览 · 已关闭',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: windowsShell({ enabled: false, status: 'off', reason: 'never_enabled' }),
    render: page,
  },
  {
    name: 'windows-on',
    title: '设置 · 电脑操作 · Windows 预览 · 已开启（就绪）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: windowsShell({ enabled: true, status: 'ready', reason: null }),
    render: page,
  },
  {
    name: 'windows-turning-on',
    title: '设置 · 电脑操作 · Windows 预览 · 正在开启',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: windowsShell({ enabled: false, status: 'off', reason: 'never_enabled' }),
    click: ['role=switch[name="{t:settings.computerUse.title}"]'],
    render: page,
  },
  {
    name: 'windows-tray-only',
    title: '设置 · 电脑操作 · Windows 预览 · 旧版桌面应用只能从托盘关闭',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: windowsShell({ enabled: true, status: 'ready', reason: null }, 'on'),
    render: page,
  },
  {
    name: 'windows-unknown',
    title: '设置 · 电脑操作 · Windows 预览 · 无法读取开关状态',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: windowsShell({ enabled: null, status: 'unavailable', reason: 'invalid_state_file' }),
    render: page,
  },
  {
    name: 'windows-browser',
    title: '设置 · 电脑操作 · Windows 预览 · 在浏览器里查看（会话已锁定）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: status({ supported: true, host_platform: 'windows', enabled: true, status: 'unavailable', reason: 'session_locked' }),
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
