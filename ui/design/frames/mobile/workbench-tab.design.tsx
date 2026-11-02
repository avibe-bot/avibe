// The renamed tab (owner decisions, 2026-11-02): 工作台 / Workbench, the Bots strip
// on top and the projects below, as in the desktop sidebar; every project row has a
// ＋ to the right of its ⋯ that starts a Session there. The strip is shipped; the
// row with the ＋ is kit. The center of the bar is the main Bot.
import { frames } from '../../frame';
import { WorkbenchTabBar, WorkbenchTabPage } from '../../kit/WorkbenchTab';
import { PhoneScreen } from '../../shell/PhoneScreen';
import { aloneFixtures, manyFixtures, teamFixtures } from '../../shell/teamFixtures';

const page = (inboxBadge: number) => (
  <PhoneScreen tabBar={<WorkbenchTabBar inboxBadge={inboxBadge} />}>
    <WorkbenchTabPage />
  </PhoneScreen>
);

const designFrames = frames([
  {
    name: 'alone',
    title: '工作台 · 只有歪比',
    width: 390,
    height: 844,
    status: 'approved',
    fixtures: aloneFixtures,
    route: '/projects',
    render: () => page(3),
  },
  {
    name: 'team',
    title: '工作台 · 4 个伙伴：未读、产品经理运行中；每个项目行 ⋯ 右边是 ＋ 新会话',
    width: 390,
    height: 844,
    status: 'approved',
    fixtures: teamFixtures,
    route: '/projects',
    render: () => page(6),
  },
  {
    name: 'many',
    title: '工作台 · 8 个伙伴：横向滑动，添加固定在右端',
    width: 390,
    height: 844,
    status: 'approved',
    fixtures: manyFixtures,
    route: '/projects',
    render: () => page(7),
  },
  {
    name: 'team-small',
    title: '工作台 · 小屏 375×667',
    width: 375,
    height: 667,
    status: 'approved',
    fixtures: teamFixtures,
    route: '/projects',
    render: () => page(6),
  },
]);

export default designFrames;
