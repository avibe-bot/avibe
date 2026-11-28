// Settings · Messages: input enhancement, Agent context, and work feedback defaults.
import { SettingsMessagingPage } from '@/components/settings/SettingsMessagingPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('messages', <SettingsMessagingPage />);
const route = '/settings/messages';

const designFrames = frames([
  {
    name: 'default',
    title: '设置 · 消息',
    width: 1440,
    height: 1520,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(),
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 消息 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures: settingsFixtures(),
    render: page,
  },
]);

export default designFrames;
