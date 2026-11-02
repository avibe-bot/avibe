// The Workbench home at `/` inside the shipped AppShell: on a desktop the sidebar, the home, and the
// sidebar's bottom cluster; on a phone the header and tab bar around it.
import { Workbench } from '@/components/Workbench';

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

// The setup wizard lands on the home with `{ onboardingCompleted: true }`; the banner shows once the
// home's backend answers ready.
const setupComplete = {
  ...fixtures,
  api: {
    ...fixtures.api,
    '/api/backend/vibey/connection': {
      ok: true,
      backend: 'vibey',
      installed: true,
      enabled: true,
      auth: 'none',
      application: 'applied',
      ready: true,
      entry_eligible: true,
    },
  },
};
const fromSetup = { pathname: '/', state: { onboardingCompleted: true } };

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
    name: 'setup-complete',
    title: '工作台 · 刚完成设置（就绪横幅）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: fromSetup,
    fixtures: setupComplete,
    render: home,
  },
  {
    name: 'setup-complete-phone',
    title: '工作台 · 刚完成设置 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: fromSetup,
    fixtures: setupComplete,
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
]);

export default designFrames;
