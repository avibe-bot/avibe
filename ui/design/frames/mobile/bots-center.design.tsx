// Variant A — the main Bot is the raised center of the tab bar. A tap opens its
// conversation from any tab; a long press opens every Bot; "new session" moves to
// the header's ＋. Pages are the shipped ones; the tab bar's center, the header
// action, and the sheet are kit proposals.
import { InboxPage } from '@/components/workbench/InboxPage';
import { NewSessionSheet } from '@/components/workbench/NewSessionSheet';
import { ProjectsPage } from '@/components/workbench/ProjectsPage';

import { frames } from '../../frame';
import { BotCenterHint, BotCenterTabBar } from '../../kit/BotCenterTabBar';
import { BotSwitchSheet } from '../../kit/BotSwitchSheet';
import { NewSessionHeaderAction } from '../../kit/NewSessionHeaderAction';
import { PhoneScreen, WithShippedTabs } from '../../shell/PhoneScreen';
import { aloneFixtures, mainRunningFixtures, teamFixtures } from '../../shell/teamFixtures';

const tabBar = (inboxBadge: number, pressed = false) => (
  <WithShippedTabs inboxBadge={inboxBadge}>{(items) => <BotCenterTabBar items={items} pressed={pressed} />}</WithShippedTabs>
);

const inbox = <InboxPage onOpenSearch={() => {}} />;

const designFrames = frames([
  {
    name: 'alone',
    title: 'A · 中间是歪比 · 只有歪比（首次提示）',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: aloneFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={tabBar(3)} overlay={<><NewSessionHeaderAction /><BotCenterHint /></>}>
        {inbox}
      </PhoneScreen>
    ),
  },
  {
    name: 'team',
    title: 'A · 中间是歪比 · 4 个伙伴：未读、运行中',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={tabBar(6)} overlay={<NewSessionHeaderAction />}>
        {inbox}
      </PhoneScreen>
    ),
  },
  {
    name: 'main-running',
    title: 'A · 中间是歪比 · 歪比在工作（项目页也一样）',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: mainRunningFixtures,
    route: '/projects',
    render: () => (
      <PhoneScreen tabBar={tabBar(5)} overlay={<NewSessionHeaderAction />}>
        <ProjectsPage />
      </PhoneScreen>
    ),
  },
  {
    name: 'long-press',
    title: 'A · 中间是歪比 · 长按：全部伙伴',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={tabBar(6, true)} overlay={<><NewSessionHeaderAction /><BotSwitchSheet aboveTabBar /></>}>
        {inbox}
      </PhoneScreen>
    ),
  },
  {
    name: 'new-session',
    title: 'A · 中间是歪比 · 新会话在页头 ＋',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={tabBar(6)} overlay={<><NewSessionHeaderAction pressed /><NewSessionSheet open onClose={() => {}} onOpen={() => {}} /></>}>
        {inbox}
      </PhoneScreen>
    ),
  },
  {
    name: 'team-small',
    title: 'A · 中间是歪比 · 小屏 375×667',
    width: 375,
    height: 667,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={tabBar(6)} overlay={<NewSessionHeaderAction />}>
        {inbox}
      </PhoneScreen>
    ),
  },
]);

export default designFrames;
