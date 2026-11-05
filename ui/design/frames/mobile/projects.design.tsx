// The phone's Projects tab: every project, expandable to its Sessions, inside the tab-bar shell.
import { ProjectsPage } from '@/components/workbench/ProjectsPage';

import { frames } from '../../frame';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { session } from '../../shell/fixtures';
import { phoneProjects, phoneSessions } from './fixtures';

const page = phoneShell('/projects', <ProjectsPage />);

// Loading without a spinner (ui/design: system/skeleton/*): the list before it arrives, and an open
// project with nothing known about its Sessions while another's Load more is pressed. Beside each, the
// same page once the rows arrived, at the same size.
const chainbotMore = [
  session('ses_bridge', 'proj_chainbot', '跨链桥风控'),
  session('ses_oracle', 'proj_chainbot', '预言机延迟排查', { agent_status: 'failed' }),
];
const openTwo = ['proj_avibe', 'proj_chainbot'];

const designFrames = frames([
  {
    name: 'loading',
    title: '手机 · 项目 · 骨架 · 首次加载',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/projects',
    fixtures: workbenchFixtures({}, { projects: phoneProjects, projectsLoading: true }),
    render: page,
  },
  {
    name: 'loading-sessions',
    title: '手机 · 项目 · 骨架 · 展开项目、加载更多',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/projects',
    fixtures: workbenchFixtures(
      {},
      {
        projects: phoneProjects,
        sessionsByProject: phoneSessions,
        expandedProjects: openTwo,
        sessionsLoading: ['proj_avibe'],
        sessionsLoadingMore: ['proj_chainbot'],
      },
    ),
    render: page,
  },
  {
    name: 'loading-sessions-loaded',
    title: '手机 · 项目 · 骨架对照 · 会话到位',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/projects',
    fixtures: workbenchFixtures(
      {},
      {
        projects: phoneProjects,
        sessionsByProject: { ...phoneSessions, proj_chainbot: [...phoneSessions.proj_chainbot, ...chainbotMore] },
        expandedProjects: openTwo,
      },
    ),
    render: page,
  },
  {
    name: 'expanded',
    title: '手机 · 项目 · 展开两个项目',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/projects',
    fixtures: workbenchFixtures(
      {},
      {
        projects: phoneProjects,
        sessionsByProject: phoneSessions,
        expandedProjects: ['proj_avibe', 'proj_chainbot'],
        unreadBySession: { ses_demo_cache: 3, ses_demo_pr: 2 },
      },
    ),
    render: page,
  },
  {
    name: 'collapsed',
    title: '手机 · 项目 · 全部收起',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/projects',
    fixtures: workbenchFixtures({}, { projects: phoneProjects, sessionsByProject: phoneSessions }),
    render: page,
  },
]);

export default designFrames;
