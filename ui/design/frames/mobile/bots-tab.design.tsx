// Variant B — Bots get a tab of their own, first in the bar, drawn with the main
// Bot's avatar. Its page leads with the main Bot and docks a one-line composer to it
// above the tab bar. The new-session ＋ keeps the center; Capabilities moves into
// the Apps drawer to make room. The page, the composer, and the tab icon are kit
// proposals; the bar is the shipped one with different items.
import { frames } from '../../frame';
import { BotsHome, BotsTabBar } from '../../kit/BotsHome';
import { PhoneScreen } from '../../shell/PhoneScreen';
import { aloneFixtures, mainRunningFixtures, teamFixtures } from '../../shell/teamFixtures';

const page = (inboxBadge: number) => (
  <PhoneScreen tabBar={<BotsTabBar inboxBadge={inboxBadge} />}>
    <BotsHome />
  </PhoneScreen>
);

const designFrames = frames([
  {
    name: 'alone',
    title: 'B · 伙伴标签 · 只有歪比',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: aloneFixtures,
    route: '/bots',
    render: () => page(3),
  },
  {
    name: 'team',
    title: 'B · 伙伴标签 · 4 个伙伴：未读、运行中',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/bots',
    render: () => page(6),
  },
  {
    name: 'main-running',
    title: 'B · 伙伴标签 · 歪比在工作',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: mainRunningFixtures,
    route: '/bots',
    render: () => page(5),
  },
  {
    name: 'team-small',
    title: 'B · 伙伴标签 · 小屏 375×667',
    width: 375,
    height: 667,
    status: 'proposed',
    fixtures: teamFixtures,
    route: '/bots',
    render: () => page(6),
  },
]);

export default designFrames;
