// Home v4 (owner, 2026-11-03 12:02; approved 19:09): one centered group, the composer its last block. Brand-new,
// partly set up, everything set up, and a member without Bots, on the desktop app at three window
// sizes (the centering depends on the height) and on two phones, plus the layout spec and a phone
// with the keyboard up. The page is kit (Home.tsx); the sidebar, the header, the tab bar (the main
// Bot in its center), the composer, and the project chips are shipped; the keyboard is the shell's stand-in.
import { MobileBrandHeader } from '@/components/MobileChrome';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames, type FrameSpec } from '../../frame';
import { SoftKeyboard } from '../../shell/HeldComposer';
import { HomePage, type SetupGap } from '../../kit/Home';
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

type State = {
  key: string;
  title: string;
  fixtures: WorkbenchFixtures;
  /** What is still missing on a desktop, and on a phone (no Computer Use or phone pairing there). */
  desktop: SetupGap[];
  phone: SetupGap[];
  tip: number;
  member?: boolean;
  inboxBadge: number;
};

const STATES: State[] = [
  { key: 'a-new', title: '全新用户：只突出「接上模型」，其余收起，建议和输入框弱化', fixtures: fresh, desktop: ['model', 'notifications', 'computerUse', 'chatApps', 'phone'], phone: ['model', 'notifications', 'chatApps'], tip: 0, inboxBadge: 0 },
  { key: 'b-partial', title: '设置了一部分：显示 2 项，其余收进「还有 1 项」，小技巧出现', fixtures: partly, desktop: ['computerUse', 'chatApps', 'phone'], phone: ['notifications', 'chatApps'], tip: 2, inboxBadge: 6 },
  { key: 'c-ready', title: '全部设置好：「开始使用」不出现，整组重新居中', fixtures: ready, desktop: [], phone: [], tip: 1, inboxBadge: 5 },
  { key: 'd-member', title: '没有伙伴的成员：同样的版面，项目选择紧贴输入框', fixtures: member, desktop: ['notifications'], phone: ['notifications'], tip: 4, member: true, inboxBadge: 3 },
];

const DESKTOPS = [
  { suffix: '', width: 1440, height: 900 },
  { suffix: '-1280', width: 1280, height: 720 },
  { suffix: '-1920', width: 1920, height: 1080 },
];
const PHONES = [
  { suffix: '', width: 390, height: 844 },
  { suffix: '-small', width: 375, height: 667 },
];

const desktop = (state: State, spec = false) => () => (
  <DesktopScreen page={<HomePage pending={state.desktop} tip={state.tip} member={state.member} spec={spec} />} />
);
const phone = (state: State, spec = false) => () => (
  <PhoneScreen bare tabBar={<ShippedTabBar inboxBadge={state.inboxBadge} />}>
    <HomePage pending={state.phone} tip={state.tip} member={state.member} compact spec={spec} />
  </PhoneScreen>
);

/** The keyboard up on a phone: the group does not re-center in the shorter visible area; the page
 *  moves up until the composer rests on the keyboard, and the keyboard covers the tab bar. */
const keyboardUp = (state: State) => () => (
  <div className="flex h-full flex-col overflow-hidden bg-background text-foreground">
    <MobileBrandHeader />
    <div className="flex min-h-0 flex-1 flex-col page-glow-console">
      <HomePage pending={state.phone} tip={state.tip} member={state.member} compact keyboard />
    </div>
    <SoftKeyboard />
  </div>
);

const stateFrames: FrameSpec[] = STATES.flatMap((state) => [
  ...DESKTOPS.map(({ suffix, width, height }) => ({
    name: `${state.key}-desktop${suffix}`,
    title: `首页 · 桌面 ${width}×${height} · ${state.title}`,
    width,
    height,
    status: 'approved' as const,
    route: '/',
    fixtures: state.fixtures,
    render: desktop(state),
  })),
  ...PHONES.map(({ suffix, width, height }) => ({
    name: `${state.key}-phone${suffix}`,
    title: `首页 · 手机 ${width}×${height} · ${state.title}`,
    width,
    height,
    status: 'approved' as const,
    route: '/',
    fixtures: state.fixtures,
    render: phone(state),
  })),
]);

const partlySetUp = STATES[1];
const allSetUp = STATES[2];

const designFrames = frames([
  ...stateFrames,
  {
    name: 'z-layout-spec',
    title: '首页 · 版面结构：居中规则（上下留白 2 : 3）、统一间距、同宽，以及所有规则',
    width: 1920,
    height: 1080,
    status: 'approved',
    route: '/',
    fixtures: partly,
    render: desktop(partlySetUp, true),
  },
  {
    name: 'z-layout-spec-phone',
    title: '首页 · 手机版面结构：同样居中，输入框是最后一块，不贴底栏',
    width: 390,
    height: 844,
    status: 'approved',
    route: '/',
    fixtures: partly,
    render: phone(partlySetUp, true),
  },
  {
    name: 'z-keyboard-phone',
    title: '首页 · 手机键盘弹起：整组不重新居中，页面整体上移，输入框贴在键盘上方，底栏被盖住',
    width: 390,
    height: 844,
    status: 'approved',
    route: '/',
    fixtures: ready,
    render: keyboardUp(allSetUp),
  },
]);

export default designFrames;
