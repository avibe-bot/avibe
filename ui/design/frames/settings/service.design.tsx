// Settings · Service: run control, the console server, the instance name, and the log file.
import { SettingsServicePage } from '@/components/settings/SettingsServicePage';

import { frames } from '../../frame';
import { settingsConfig, settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('service', <SettingsServicePage />);
const route = '/settings/service';

// Bound to an account, with a network address as the console server's host.
const boundConfig = {
  ...settingsConfig,
  ui: { ...settingsConfig.ui, setup_host: '192.168.1.20' },
  remote_access: {
    ...settingsConfig.remote_access,
    vibe_cloud: { ...settingsConfig.remote_access.vibe_cloud, instance_id: 'inst_demo' },
  },
};

const designFrames = frames([
  {
    name: 'default',
    title: '设置 · 服务 · 运行中',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(),
    render: page,
  },
  {
    name: 'stopped',
    title: '设置 · 服务 · 已停止',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({}, { status: { state: 'stopped' } }),
    render: page,
  },
  {
    name: 'bound',
    title: '设置 · 服务 · 已绑定账号',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({ '/api/config': boundConfig }),
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 服务 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(),
    render: page,
  },
]);

export default designFrames;
