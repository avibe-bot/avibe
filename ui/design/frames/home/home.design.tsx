// Home v3 (owner, 2026-11-03 02:34): the composer at the bottom, where a conversation's
// is, in every state; above it one order, one width, and one rule for an absent block.
// Brand-new, partly set up, everything set up, and a member without Bots, on the desktop
// app and on a phone, plus the layout spec. The page is kit (Home.tsx); the sidebar, the
// tab bar, the composer, and the project chips are shipped.
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

const desktop = (pending: SetupGap[], tip: number, isMember = false, spec = false) => () => (
  <DesktopScreen page={<HomePage pending={pending} tip={tip} member={isMember} spec={spec} />} />
);
const phone = (pending: SetupGap[], tip: number, inboxBadge: number) => () => (
  <PhoneScreen bare tabBar={<WorkbenchTabBar inboxBadge={inboxBadge} />}>
    <HomePage pending={pending} tip={tip} compact />
  </PhoneScreen>
);

const D = { width: 1440, height: 900, status: 'proposed' as const, route: '/' };
const P = { width: 390, height: 844, status: 'proposed' as const, route: '/' };

const designFrames = frames([
  { ...D, name: 'a-new-desktop', title: '首页 · 全新用户：只突出「接上模型」，其余 4 项收起，输入框和建议弱化', fixtures: fresh, render: desktop(['model', 'notifications', 'computerUse', 'chatApps', 'phone'], 0) },
  { ...P, name: 'a-new-phone', title: '首页 · 手机 · 全新用户', fixtures: fresh, render: phone(['model', 'notifications', 'chatApps'], 0, 0) },
  { ...D, name: 'b-partial-desktop', title: '首页 · 设置了一部分：显示 2 项，其余收进「还有 1 项」，小技巧出现', fixtures: partly, render: desktop(['computerUse', 'chatApps', 'phone'], 2) },
  { ...P, name: 'b-partial-phone', title: '首页 · 手机 · 设置了一部分', fixtures: partly, render: phone(['notifications', 'chatApps'], 2, 6) },
  { ...D, name: 'c-ready-desktop', title: '首页 · 全部设置好：「开始使用」不占位，上面的问候下移，下面的不动', fixtures: ready, render: desktop([], 1) },
  { ...P, name: 'c-ready-phone', title: '首页 · 手机 · 全部设置好', fixtures: ready, render: phone([], 1, 5) },
  { ...D, name: 'd-member-desktop', title: '首页 · 没有伙伴的成员：同样的版面，输入框上方选项目', fixtures: member, render: desktop(['notifications'], 4, true) },
  {
    ...P,
    name: 'd-member-phone',
    title: '首页 · 手机 · 没有伙伴的成员（底栏中间仍是新会话）',
    fixtures: member,
    render: () => (
      <PhoneScreen bare tabBar={<ShippedTabBar inboxBadge={3} />}>
        <HomePage pending={['notifications']} tip={4} member compact />
      </PhoneScreen>
    ),
  },
  { ...D, name: 'z-layout-spec', title: '首页 · 版面结构：区块顺序、缺席规则、宽度和间距（所有区块都出现时）', fixtures: partly, render: desktop(['computerUse', 'chatApps', 'phone'], 2, false, true) },
]);

export default designFrames;
