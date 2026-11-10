// The Workbench home at `/` inside the shipped AppShell: on a desktop the sidebar, the home, and the
// sidebar's bottom cluster; on a phone the header and tab bar around it. Its states at every size
// are `home/home/*`; these are the shell around it and two Bots at work, listed to pick from.
import { Workbench } from '@/components/Workbench';

import { frames } from '../../frame';
import { demoSessions, inboxRow } from '../../shell/fixtures';
import { agentsApi } from '../agents/fixtures';
import { homeFactsApi, phoneShell, workbenchFixtures } from './fixtures';

const home = phoneShell('/', <Workbench />);

// Everything set up, so the shell is what shows.
const fixtures = workbenchFixtures({ ...agentsApi(), ...homeFactsApi({ model: true, push: false, computerUse: true, chatApps: true, paired: true }) }, {
  sessionsByProject: demoSessions,
  expandedProjects: ['proj_avibe'],
  unreadBySession: { ses_demo_cache: 3, ses_cso: 2 },
  inboxSessions: [
    inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞。', 6, 2),
    inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR 已开。', 12, 3),
  ],
});
// Two Bots at work: "2 working" lists them to pick from.
const busy = { ...fixtures, botStatus: { ses_main: 'running' as const, ses_pm: 'running' as const } };
const openBusy = ['main >> text=/^2 (位在忙|working)$/'];
// What the shell says above every page when the service is not in the user's desktop session.
const withLoginSession = (state: 'outside_gui_session' | 'stale_login_session') => ({
  ...fixtures,
  api: { ...fixtures.api, '/api/macos/login-session': { ok: true, state } },
});

const designFrames = frames([
  {
    name: 'desktop',
    title: '工作台 · 桌面（侧边栏 + 首页）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures,
    render: home,
  },
  {
    name: 'desktop-busy',
    title: '工作台 · 桌面 · 两位伙伴在忙：点「2 位在忙」列出它们',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: busy,
    click: openBusy,
    render: home,
  },
  {
    name: 'phone',
    title: '工作台 · 手机首页',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/',
    fixtures,
    render: home,
  },
  {
    name: 'phone-busy',
    title: '工作台 · 手机 · 两位伙伴在忙：底部列表',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/',
    fixtures: busy,
    click: openBusy,
    render: home,
  },
  {
    name: 'desktop-outside-session',
    title: '工作台 · 桌面 · 服务运行在桌面会话之外：钥匙串凭据不可用（警告）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: withLoginSession('outside_gui_session'),
    render: home,
  },
  {
    name: 'phone-outside-session',
    title: '工作台 · 手机 · 服务运行在桌面会话之外',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/',
    fixtures: withLoginSession('outside_gui_session'),
    render: home,
  },
  {
    name: 'desktop-stale-session',
    title: '工作台 · 桌面 · 服务所在的登录会话已结束（错误）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: withLoginSession('stale_login_session'),
    render: home,
  },
]);

export default designFrames;
