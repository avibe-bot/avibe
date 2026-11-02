// Settings · Direct messages: the bind code, then every user bound on each platform with their route.
import { UserList } from '@/components/steps/UserList';

import { frames } from '../../frame';
import { modelMenusApi } from '../agents/fixtures';
import { bindCodesApi, scopeSettingsApi, settingsFixtures, settingsPage, usersApi } from './fixtures';

const page = settingsPage('platforms/users', <UserList />);
const fixtures = settingsFixtures({ ...scopeSettingsApi, ...usersApi, ...bindCodesApi });

const designFrames = frames([
  {
    name: 'default',
    title: '设置 · 私聊 · 已绑定用户',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/platforms/users',
    fixtures,
    render: page,
  },
  {
    name: 'expanded',
    title: '设置 · 私聊 · 展开一位用户的路由',
    width: 1440,
    height: 1100,
    status: 'shipped',
    // The deep link a chat uses to open one user's row.
    route: '/settings/platforms/users?platform=slack&user=U04ALEXLIU',
    fixtures,
    render: page,
  },
  {
    name: 'empty',
    title: '设置 · 私聊 · 还没有用户',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/platforms/users',
    fixtures: settingsFixtures({
      ...scopeSettingsApi,
      '/api/users': { ok: true, users: {} },
      '/api/bind-codes': { ok: true, bind_codes: [] },
    }),
    render: page,
  },
  {
    name: 'route-picker',
    title: '设置 · 私聊 · Agent 路由选择器（三栏级联）',
    width: 1440,
    height: 1100,
    status: 'shipped',
    // Marc's route runs the reviewer Agent on Claude Code, so all three columns have choices.
    route: '/settings/platforms/users?platform=telegram&user=58181121',
    fixtures: { ...fixtures, api: { ...fixtures.api, ...modelMenusApi } },
    click: ['button[aria-haspopup="dialog"]:has-text("reviewer")'],
    render: page,
  },
  {
    name: 'phone',
    title: '设置 · 私聊 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/settings/platforms/users',
    fixtures,
    render: page,
  },
]);

export default designFrames;
