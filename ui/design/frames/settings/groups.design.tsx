// Settings · Groups: every group the connected platforms report, with its on/off switch and route.
import { ChannelList } from '@/components/steps/ChannelList';

import { frames } from '../../frame';
import { platformChannelsApi, scopeSettingsApi, settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('platforms/groups', <ChannelList isPage />);
const route = '/settings/platforms/groups';
const fixtures = settingsFixtures({ ...scopeSettingsApi, ...platformChannelsApi });

const designFrames = frames([
  {
    name: 'default',
    title: '设置 · 群组 · 全部平台',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures,
    render: page,
  },
  {
    name: 'empty',
    title: '设置 · 群组 · 还没有发现群组',
    width: 1440,
    height: 900,
    status: 'shipped',
    route,
    fixtures: settingsFixtures({
      ...scopeSettingsApi,
      '/api/slack/channels': { ok: true, channels: [] },
      '/api/discord/channels': { ok: true, channels: [] },
      '/api/telegram/chats': { ok: true, channels: [] },
    }),
    render: page,
  },
  {
    name: 'expanded',
    title: '设置 · 群组 · 展开一个群组的路由',
    width: 1440,
    height: 1000,
    status: 'shipped',
    route,
    fixtures,
    click: ['text=ai-bots'],
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 群组 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route,
    fixtures,
    render: page,
  },
]);

export default designFrames;
