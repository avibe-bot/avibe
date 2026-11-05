// The shipped sidebar, rendered by the shipped component from fixtures.
import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';

import { frames } from '../../frame';
import { demoProjects, demoSessions, inboxRow, mainBot, session } from '../../shell/fixtures';
import { SidebarColumn } from '../../shell/SidebarColumn';

const sidebar = () => (
  <SidebarColumn>
    <WorkbenchSidebar />
  </SidebarColumn>
);

// Loading without a spinner (ui/design: system/skeleton/*). `avibe` is opened with nothing known about
// its Sessions; `chainbot` had two and Load more was pressed. Each skeleton frame sits beside the same
// sidebar once the rows arrived, at the same size, so the rows can be checked to land where the
// blocks were.
const chainbotFirst = [
  session('ses_audit', 'proj_chainbot', '合约审计'),
  session('ses_gas', 'proj_chainbot', 'Gas 优化方案', { agent_status: 'running' }),
];
const chainbotMore = [
  session('ses_bridge', 'proj_chainbot', '跨链桥风控'),
  session('ses_oracle', 'proj_chainbot', '预言机延迟排查', { agent_status: 'failed' }),
];
const loadingSessions = {
  projects: demoProjects,
  bots: [mainBot],
  expandedProjects: ['proj_avibe', 'proj_chainbot'],
  sessionsLoading: ['proj_avibe'],
  sessionsByProject: { proj_chainbot: chainbotFirst },
  sessionsLoadingMore: ['proj_chainbot'],
};
const loadedSessions = {
  projects: demoProjects,
  bots: [mainBot],
  expandedProjects: ['proj_avibe', 'proj_chainbot'],
  sessionsByProject: { ...demoSessions, proj_chainbot: [...chainbotFirst, ...chainbotMore] },
};

const designFrames = frames([
  {
    name: 'loading-projects',
    title: '侧边栏 · 骨架 · 项目列表首次加载',
    width: 248,
    height: 640,
    status: 'shipped',
    fixtures: { projects: demoProjects, projectsLoading: true, bots: [mainBot] },
    render: sidebar,
  },
  {
    name: 'loading-projects-loaded',
    title: '侧边栏 · 骨架对照 · 项目列表到位',
    width: 248,
    height: 640,
    status: 'shipped',
    fixtures: { projects: demoProjects, bots: [mainBot] },
    render: sidebar,
  },
  {
    name: 'loading-sessions',
    title: '侧边栏 · 骨架 · 展开项目、加载更多',
    width: 248,
    height: 800,
    status: 'shipped',
    fixtures: loadingSessions,
    render: sidebar,
  },
  {
    name: 'loading-sessions-loaded',
    title: '侧边栏 · 骨架对照 · 会话到位',
    width: 248,
    height: 800,
    status: 'shipped',
    fixtures: loadedSessions,
    render: sidebar,
  },
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
