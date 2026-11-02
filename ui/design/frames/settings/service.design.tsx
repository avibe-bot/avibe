// Settings · Service: run control, the console server, the instance name, and the log file.
import { SettingsServicePage } from '@/components/settings/SettingsServicePage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('service', <SettingsServicePage />);
const route = '/settings/service';

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
