// The Harness page: scheduled Tasks, Watches, and the Runs they produce, with a run's detail beside the list.
import { HarnessPage } from '@/components/workbench/HarnessPage';

import { frames } from '../../frame';
import { consolePage, phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { harnessApi } from './fixtures';

const page = <HarnessPage />;
const none = { tasks: [], watches: [], runs: [] };

const designFrames = frames([
  {
    name: 'tasks',
    title: 'Harness · 后台任务',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/harness?tab=tasks',
    fixtures: workbenchFixtures(harnessApi('tasks')),
    render: consolePage(page),
  },
  {
    name: 'watches',
    title: 'Harness · 等待器',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/harness?tab=watches',
    fixtures: workbenchFixtures(harnessApi('watches')),
    render: consolePage(page),
  },
  {
    name: 'run-detail',
    title: 'Harness · 运行记录与运行详情',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/harness?tab=runs&run=run_x8m2q4p7',
    fixtures: workbenchFixtures(harnessApi('runs')),
    render: consolePage(page),
  },
  {
    name: 'empty',
    title: 'Harness · 还没有后台任务',
    width: 1192,
    height: 700,
    status: 'shipped',
    route: '/harness?tab=tasks',
    fixtures: workbenchFixtures(harnessApi('tasks', none)),
    render: consolePage(page),
  },
  {
    name: 'task-detail',
    title: 'Harness · 选中一个后台任务',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/harness?tab=tasks',
    fixtures: workbenchFixtures(harnessApi('tasks')),
    click: ['text=Slack #ops 每天 09:30 战报'],
    render: consolePage(page),
  },
  {
    name: 'phone',
    title: 'Harness · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/harness?tab=tasks',
    fixtures: workbenchFixtures(harnessApi('tasks')),
    render: phoneShell('/harness', page),
  },
]);

export default designFrames;
