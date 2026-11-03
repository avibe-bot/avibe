// The Workbench home at `/` inside the shipped AppShell: on a desktop the sidebar, the home, and the
// sidebar's bottom cluster; on a phone the header and tab bar around it.
import { Workbench } from '@/components/Workbench';

import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames } from '../../frame';
import { demoSessions, inboxRow } from '../../shell/fixtures';
import { agentsApi } from '../agents/fixtures';
import { phoneShell, workbenchFixtures } from './fixtures';

const home = phoneShell('/', <Workbench />);

const fixtures = workbenchFixtures(agentsApi(), {
  sessionsByProject: demoSessions,
  expandedProjects: ['proj_avibe'],
  unreadBySession: { ses_demo_cache: 3, ses_cso: 2 },
  inboxSessions: [
    inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞。', 6, 2),
    inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR 已开。', 12, 3),
  ],
});

// The main Bot's backend as `/api/backend/<backend>/connection` reports it: a model is connected
// (an API key supplies it), or none is (the composer waits for one).
const connection = (auth: 'api_key' | 'none') => ({
  '/api/backend/vibey/connection': {
    ok: true, backend: 'vibey', installed: true, enabled: true, supply_mode: 'hub',
    auth, application: 'applied', ready: auth !== 'none', entry_eligible: auth !== 'none',
  },
});
const ready = { ...fixtures, api: { ...fixtures.api, ...connection('api_key') } };
const noModel = { ...fixtures, api: { ...fixtures.api, ...connection('none') } };
// Two Bots at work: "2 working" lists them to pick from.
const busy = { ...ready, botStatus: { ses_main: 'running' as const, ses_pm: 'running' as const } };
// A member without Bots: the project chips stand right above the composer.
const member = {
  ...ready,
  capabilities: { ...OWNER_INSTANCE_CAPABILITIES, is_instance_owner: false, can_manage_instance: false },
  bots: [],
};

const designFrames = frames([
  {
    name: 'desktop',
    title: '工作台 · 桌面（侧边栏 + 首页）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: ready,
    render: home,
  },
  {
    name: 'desktop-1280',
    title: '工作台 · 桌面 1280×720',
    width: 1280,
    height: 720,
    status: 'shipped',
    route: '/',
    fixtures: ready,
    render: home,
  },
  {
    name: 'desktop-no-model',
    title: '工作台 · 桌面 · 还没有模型：问候自我介绍，建议变灰，输入框禁用并给出「去接模型」',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: noModel,
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
    click: ['main >> text=/^2 (位在忙|working)$/'],
    render: home,
  },
  {
    name: 'desktop-member',
    title: '工作台 · 桌面 · 没有伙伴的成员：项目选择紧贴输入框',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: member,
    render: home,
  },
  {
    name: 'phone',
    title: '工作台 · 手机首页',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/',
    fixtures: ready,
    render: home,
  },
  {
    name: 'phone-small',
    title: '工作台 · 手机 375×667',
    width: 375,
    height: 667,
    status: 'shipped',
    route: '/',
    fixtures: ready,
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
    click: ['main >> text=/^2 (位在忙|working)$/'],
    render: home,
  },
  {
    name: 'phone-no-model',
    title: '工作台 · 手机 · 还没有模型',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/',
    fixtures: noModel,
    render: home,
  },
]);

export default designFrames;
