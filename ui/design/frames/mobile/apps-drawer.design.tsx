// The phone's Apps tab: the Dock drawer over the current page, and its More sheet (appearance, status).
// The drawer ends at the tab bar's top edge. The main Bot's circle, which rises above that edge by
// `--mobile-nav-center-rise`, always draws over the drawer, whole; the drawer's foot keeps that rise
// plus 16px, so its last row (设置 / 账号 / 更多) is never under the circle (owner request,
// 2026-11-07). Anything opened over the drawer (a take started by holding the circle, the
// new-session sheet) draws above both the drawer and the bar. The More and Account sheets and the long-press sheet cover the bar instead:
// they are modal over everything, the bar included.
import { MobileDockDrawer } from '@/components/apps/MobileDockDrawer';
import { InboxPage } from '@/components/workbench/InboxPage';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames } from '../../frame';
import { BotCenterTabBar, MainBotVoiceLayer, PhoneScreen } from '../../shell/PhoneScreen';
import { spoken, teamFixtures } from '../../shell/teamFixtures';
import { showPagesApi } from '../show-pages/fixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

const drawer = phoneShell(
  '/inbox',
  <>
    <InboxPage onOpenSearch={() => {}} />
    <MobileDockDrawer open onClose={() => {}} />
  </>,
);

/** A member: no Bots, so the bar's center is the new-session ＋, which does not rise; the drawer keeps the same foot. */
const member = workbenchFixtures(showPagesApi(), {
  capabilities: { ...OWNER_INSTANCE_CAPABILITIES, is_instance_owner: false, can_manage_instance: false },
  bots: [],
  botStatus: {},
});

/** Holding the main Bot with the drawer open: the bar, the take, and the drawer in AppShell's order. */
const holding = () => (
  <PhoneScreen
    tabBar={<BotCenterTabBar phase="recording" />}
    overlay={
      <>
        <MainBotVoiceLayer hold={{ phase: 'recording', text: spoken.text, stash: spoken.stash, seconds: 6 }} />
        <MobileDockDrawer open onClose={() => {}} />
      </>
    }
  >
    <InboxPage onOpenSearch={() => {}} />
  </PhoneScreen>
);

const designFrames = frames([
  {
    name: 'open',
    title: '手机 · 应用抽屉：中间的歪比完整压在抽屉上面，最后一排留出它凸出的高度 + 16px',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures(showPagesApi()),
    render: drawer,
  },
  {
    name: 'small',
    title: '手机 · 应用抽屉 · 小屏 375×667',
    width: 375,
    height: 667,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures(showPagesApi()),
    render: drawer,
  },
  {
    name: 'member',
    title: '手机 · 应用抽屉 · 没有伙伴的成员：中间是 ＋，抽屉底部留白不变',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: member,
    render: drawer,
  },
  {
    name: 'hold',
    title: '手机 · 应用抽屉开着时按住歪比说话：录音卡片在抽屉上面',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures({ ...teamFixtures.api, ...showPagesApi() }, teamFixtures),
    render: holding,
  },
  {
    name: 'more',
    title: '手机 · 应用抽屉 · 更多：盖住整个底栏的模态面板',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures(showPagesApi()),
    click: ['role=button[name="{t:nav.more}"]'],
    render: drawer,
  },
]);

export default designFrames;
