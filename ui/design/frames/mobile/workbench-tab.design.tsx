// The renamed tab (owner direction, 2026-11-02): 工作台 / Workbench, the Bots strip
// on top and the projects below, as in the desktop sidebar. The strip and the
// project list are the shipped components; the center of the bar is the main Bot.
import { frames } from '../../frame';
import { WorkbenchTabBar, WorkbenchTabPage } from '../../kit/WorkbenchTab';
import { PhoneScreen } from '../../shell/PhoneScreen';
import { aloneFixtures, manyFixtures, teamFixtures } from '../../shell/teamFixtures';

const page = (inboxBadge: number, tip = false) => (
  <PhoneScreen tabBar={<WorkbenchTabBar inboxBadge={inboxBadge} />}>
    <WorkbenchTabPage tip={tip} />
  </PhoneScreen>
);

const designFrames = frames([
  {
    name: 'alone',
    title: '工作台 · 只有歪比',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: aloneFixtures,
    route: '/projects',
    render: () => page(3),
  },
  {
    name: 'team',
    title: '工作台 · 4 个伙伴：未读、产品经理运行中',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/projects',
    render: () => page(6),
  },
  {
    name: 'many',
    title: '工作台 · 8 个伙伴：横向滑动，添加固定在右端',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: manyFixtures,
    route: '/projects',
    render: () => page(7),
  },
  {
    name: 'new-session',
    title: '工作台 · 新会话从项目开始（一次性提示）',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/projects',
    render: () => page(6, true),
  },
  {
    name: 'new-session-menu',
    title: '工作台 · 新会话：项目行 ⋯ → 新建会话（已上线）',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/projects',
    click: ['role=button[name="{t:workbench.projectActions}"] >> nth=0'],
    render: () => page(6),
  },
  {
    name: 'team-small',
    title: '工作台 · 小屏 375×667',
    width: 375,
    height: 667,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/projects',
    render: () => page(6),
  },
]);

export default designFrames;
