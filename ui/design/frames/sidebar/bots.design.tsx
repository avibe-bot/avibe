// Bots in the sidebar (docs/plans/bots.md D5): the shipped strip, hover card, and Inbox icon,
// drawn by the shipped sidebar from fixtures. The main Bot's mascot has no well, here as
// anywhere (owner, 2026-11-04); the other Bots follow it in the server's order (D13). With
// every place for a custom Bot taken (D12) the strip has no Add: its room goes to the Bots,
// and the Agents page says why adding waits.
import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';

import { frames } from '../../frame';
import { demoBots, demoProjects, inboxRow, mainBot, type WorkbenchFixtures } from '../../shell/fixtures';
import { fullTeam } from '../../shell/teamFixtures';
import { SidebarColumn } from '../../shell/SidebarColumn';

const sidebar = () => (
  <SidebarColumn>
    <WorkbenchSidebar />
  </SidebarColumn>
);

const team: WorkbenchFixtures = {
  projects: demoProjects,
  bots: demoBots,
  botStatus: { ses_pm: 'running' },
  unreadBySession: { ses_cso: 2, ses_demo_cache: 3 },
  inboxSessions: [inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞', 20, 2)],
  // The hover card reads the Bot's latest message when it opens.
  api: {
    '/api/inbox?limit=1&session=ses_pm': {
      sessions: [inboxRow('ses_pm', null, '产品经理', '需求池按优先级排好了，前三项需要你拍板。', 2)],
    },
  },
};

const designFrames = frames([
  {
    name: 'new-user',
    title: '伙伴 · 只有歪比',
    width: 248,
    height: 360,
    status: 'shipped',
    fixtures: { projects: demoProjects, bots: [mainBot] },
    render: sidebar,
  },
  {
    name: 'three',
    title: '伙伴 · 3 个伙伴：未读、运行中',
    width: 248,
    height: 360,
    status: 'shipped',
    fixtures: team,
    render: sidebar,
  },
  {
    name: 'at-limit',
    title: '伙伴 · 刚好 5 位伙伴：没有「添加」，头像横向滑动',
    width: 248,
    height: 360,
    status: 'shipped',
    fixtures: { ...team, bots: fullTeam },
    render: sidebar,
  },
  {
    name: 'main-open',
    title: '伙伴 · 在歪比的对话里：歪比没有底色，选中的细圈画在同样大小的圆周上',
    width: 248,
    height: 360,
    status: 'shipped',
    fixtures: team,
    route: '/chat/ses_main',
    render: sidebar,
  },
  {
    name: 'hover-card',
    title: '伙伴 · 悬停卡片',
    width: 520,
    height: 360,
    status: 'shipped',
    fixtures: team,
    hover: '[data-bot-handle="pm"]',
    render: () => (
      <div className="h-full w-[248px]">
        {sidebar()}
      </div>
    ),
  },
]);

export default designFrames;
