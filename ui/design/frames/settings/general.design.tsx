// Settings · General: interface language, appearance, and where the Settings menu sits, each saved as
// it is picked. The desktop app's Settings… menu item opens this page. The appearance follows the
// frame's theme.
import { SettingsGeneralPage } from '@/components/settings/SettingsGeneralPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('general', <SettingsGeneralPage />);
const route = '/settings/general';

const designFrames = frames([
  {
    name: 'default',
    title: '设置 · 常规 · 语言与外观',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(),
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 常规 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(),
    render: page,
  },
]);

export default designFrames;
