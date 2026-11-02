// The phone's Projects tab: every project, expandable to its Sessions, inside the tab-bar shell.
import { ProjectsPage } from '@/components/workbench/ProjectsPage';

import { frames } from '../../frame';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { phoneProjects, phoneSessions } from './fixtures';

const page = phoneShell('/projects', <ProjectsPage />);

const designFrames = frames([
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
