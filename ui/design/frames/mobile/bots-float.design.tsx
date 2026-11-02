// Variant C — the main Bot floats over every tabbed page, like the desktop pet: in
// the right thumb's reach above the tab bar, speaking only when it has news, and
// opening a peek sheet with every Bot as a chip. The shipped tab bar and its
// new-session ＋ are unchanged. The bubble and the sheet are kit proposals.
import { InboxPage } from '@/components/workbench/InboxPage';
import { ProjectsPage } from '@/components/workbench/ProjectsPage';

import { frames } from '../../frame';
import { FloatingMainBot, MainBotPeekSheet } from '../../kit/FloatingMainBot';
import { PhoneScreen, ShippedTabBar } from '../../shell/PhoneScreen';
import { aloneFixtures, mainRunningFixtures, teamFixtures } from '../../shell/teamFixtures';

const inbox = <InboxPage onOpenSearch={() => {}} />;

const designFrames = frames([
  {
    name: 'alone',
    title: 'C · 歪比悬浮 · 只有歪比、没有新消息',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: aloneFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={<ShippedTabBar inboxBadge={3} />} overlay={<FloatingMainBot />}>
        {inbox}
      </PhoneScreen>
    ),
  },
  {
    name: 'team',
    title: 'C · 歪比悬浮 · 有一条未读回复',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={<ShippedTabBar inboxBadge={6} />} overlay={<FloatingMainBot />}>
        {inbox}
      </PhoneScreen>
    ),
  },
  {
    name: 'main-running',
    title: 'C · 歪比悬浮 · 歪比在工作（项目页）',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: mainRunningFixtures,
    route: '/projects',
    render: () => (
      <PhoneScreen tabBar={<ShippedTabBar inboxBadge={5} />} overlay={<FloatingMainBot />}>
        <ProjectsPage />
      </PhoneScreen>
    ),
  },
  {
    name: 'peek',
    title: 'C · 歪比悬浮 · 点开：速览与全部伙伴',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={<ShippedTabBar inboxBadge={6} />} overlay={<MainBotPeekSheet />}>
        {inbox}
      </PhoneScreen>
    ),
  },
  {
    name: 'peek-switch',
    title: 'C · 歪比悬浮 · 速览里切到首席安全官',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={<ShippedTabBar inboxBadge={6} />} overlay={<MainBotPeekSheet current="cso" />}>
        {inbox}
      </PhoneScreen>
    ),
  },
  {
    name: 'team-small',
    title: 'C · 歪比悬浮 · 小屏 375×667',
    width: 375,
    height: 667,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={<ShippedTabBar inboxBadge={6} />} overlay={<FloatingMainBot />}>
        {inbox}
      </PhoneScreen>
    ),
  },
]);

export default designFrames;
