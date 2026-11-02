// The shipped sidebar, rendered by the shipped component from fixtures.
import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';

import { frames } from '../../frame';
import { demoProjects, demoSessions, inboxRow, mainBot } from '../../shell/fixtures';
import { SidebarColumn } from '../../shell/SidebarColumn';

const sidebar = () => (
  <SidebarColumn>
    <WorkbenchSidebar />
  </SidebarColumn>
);

const designFrames = frames([
  {
    name: 'default',
    title: '侧边栏 · 现状',
    width: 248,
    height: 640,
    status: 'shipped',
    fixtures: { projects: demoProjects, bots: [mainBot] },
    render: sidebar,
  },
  {
    name: 'unread-expanded',
    title: '侧边栏 · 现状 · 有未读、展开项目',
    width: 248,
    height: 640,
    status: 'shipped',
    fixtures: {
      projects: demoProjects,
      bots: [mainBot],
      sessionsByProject: demoSessions,
      expandedProjects: ['proj_avibe'],
      unreadBySession: { ses_demo_cache: 3, ses_demo_pr: 2 },
      inboxSessions: [
        inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR 已开。', 5, 3),
        inboxRow('ses_demo_pr', 'avibe', 'PR #3073 交付', 'CI 失败：unit-tests (2/8)。', 30, 2),
      ],
    },
    render: sidebar,
  },
  {
    name: 'skills-selected',
    title: '侧边栏 · Skills 选中、Agents 鼠标移上',
    width: 248,
    height: 640,
    status: 'shipped',
    route: '/skills',
    fixtures: { projects: demoProjects, bots: [mainBot] },
    hover: '#workbench-capability-nav a[href="/agents"]',
    render: sidebar,
  },
]);

export default designFrames;
