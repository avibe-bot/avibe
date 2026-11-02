// Home (owner direction, 2026-11-02 15:48): a page that talks to the main Bot with no
// project to pick, walks a new user through what is missing, and teaches one
// capability at a time. Brand-new, partly set up, everything set up, and a member
// without Bots, on the desktop app and on a phone. The page is kit (Home.tsx); the
// sidebar, the tab bar's center, the composer, and the project chips are shipped.
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames } from '../../frame';
import { HomePage, type SetupGap } from '../../kit/Home';
import { WorkbenchTabBar } from '../../kit/WorkbenchTab';
import { DesktopScreen } from '../../shell/DesktopScreen';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { PhoneScreen, ShippedTabBar } from '../../shell/PhoneScreen';
import { agentsApi, aloneFixtures, mainRunningFixtures, teamFixtures } from '../../shell/teamFixtures';

const fresh: WorkbenchFixtures = { ...aloneFixtures, unreadBySession: {}, inboxSessions: [], api: agentsApi };
const partly: WorkbenchFixtures = { ...teamFixtures, api: agentsApi };
const ready: WorkbenchFixtures = { ...mainRunningFixtures, api: agentsApi };
const member: WorkbenchFixtures = {
  ...teamFixtures,
  capabilities: { ...OWNER_INSTANCE_CAPABILITIES, is_instance_owner: false, can_manage_instance: false },
  bots: [],
  unreadBySession: { ses_demo_cache: 3 },
  inboxSessions: [],
  api: agentsApi,
};

const desktop = (pending: SetupGap[], tip: number, isMember = false) => () => (
  <DesktopScreen page={<HomePage pending={pending} tip={tip} member={isMember} />} />
);
const phone = (pending: SetupGap[], tip: number, inboxBadge: number) => () => (
  <PhoneScreen tabBar={<WorkbenchTabBar inboxBadge={inboxBadge} />}>
    <HomePage pending={pending} tip={tip} compact />
  </PhoneScreen>
);

const D = { width: 1440, height: 900, status: 'proposed' as const, route: '/' };
const P = { width: 390, height: 844, status: 'proposed' as const, route: '/' };

const designFrames = frames([
  { ...D, name: 'a-new-desktop', title: '首页 · 全新用户（桌面应用）：还有 5 步，模型是第一步', fixtures: fresh, render: desktop(['model', 'notifications', 'computerUse', 'chatApps', 'phone'], 0) },
  { ...P, name: 'a-new-phone', title: '首页 · 手机 · 全新用户：还有 3 步', fixtures: fresh, render: phone(['model', 'notifications', 'chatApps'], 0, 0) },
  { ...D, name: 'b-partial-desktop', title: '首页 · 设置了一部分：模型和通知已好，还差 2 步', fixtures: partly, render: desktop(['computerUse', 'chatApps'], 2) },
  { ...P, name: 'b-partial-phone', title: '首页 · 手机 · 设置了一部分', fixtures: partly, render: phone(['chatApps'], 2, 6) },
  { ...D, name: 'c-ready-desktop', title: '首页 · 全部设置好：安静的首页，一行正在发生的事和一条小技巧', fixtures: ready, render: desktop([], 1) },
  { ...P, name: 'c-ready-phone', title: '首页 · 手机 · 全部设置好', fixtures: ready, render: phone([], 1, 5) },
  { ...D, name: 'd-member-desktop', title: '首页 · 没有伙伴的成员：同样的结构，输入框在项目里开会话', fixtures: member, render: desktop(['notifications'], 4, true) },
  {
    ...P,
    name: 'd-member-phone',
    title: '首页 · 手机 · 没有伙伴的成员（底栏中间仍是新会话）',
    fixtures: member,
    render: () => (
      <PhoneScreen tabBar={<ShippedTabBar inboxBadge={3} />}>
        <HomePage pending={['notifications']} tip={4} member compact />
      </PhoneScreen>
    ),
  },
]);

export default designFrames;
