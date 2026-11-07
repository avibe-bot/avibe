// Home v4 (owner, 2026-11-03; shipped): the home inside the shipped AppShell, brand-new, partly set
// up, everything set up, and a member without Bots, on the desktop app at three window sizes (the
// centering depends on the height) and on two phones, plus browser and native-shell phones with the
// keyboard up. What each state shows comes from the facts the home reads (the main Bot's backend,
// this device's push, the desktop app's Computer Use, the config's chat apps, Avibe Cloud pairing)
// and the account's home preferences, served by the fixtures below. The layout's rules are in
// `HomeLayout`.
import { MobileBrandHeader } from '@/components/MobileChrome';
import { Workbench } from '@/components/Workbench';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { frames, type FrameSpec } from '../../frame';
import { SoftKeyboard } from '../../shell/HeldComposer';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { agentsApi, aloneFixtures, mainRunningFixtures, teamFixtures } from '../../shell/teamFixtures';
import { homeFactsApi, phoneShell, workbenchFixtures, type HomeFacts } from '../workbench/fixtures';

type State = {
  key: string;
  title: string;
  base: WorkbenchFixtures;
  facts: HomeFacts;
  member?: boolean;
};

const memberCapabilities = { ...OWNER_INSTANCE_CAPABILITIES, is_instance_owner: false, can_manage_instance: false };

const STATES: State[] = [
  {
    key: 'a-new',
    title: '全新用户：只突出「接上模型」，其余收起，建议变灰，输入框禁用',
    base: { ...aloneFixtures, unreadBySession: {}, inboxSessions: [] },
    facts: { model: false, push: true, computerUse: false, chatApps: false, paired: false },
  },
  {
    key: 'b-partial',
    title: '设置了一部分：显示 2 项，其余收进「还有 N 项」，小技巧出现',
    base: teamFixtures,
    facts: { model: true, push: false, computerUse: false, chatApps: false, paired: false },
  },
  {
    key: 'c-ready',
    title: '全部设置好：「开始使用」不出现，整组重新居中',
    base: mainRunningFixtures,
    facts: { model: true, push: false, computerUse: true, chatApps: true, paired: true },
  },
  {
    key: 'd-member',
    title: '没有伙伴的成员：同样的版面，项目选择紧贴输入框',
    base: { ...teamFixtures, capabilities: memberCapabilities, bots: [], unreadBySession: { ses_demo_cache: 3 }, inboxSessions: [] },
    facts: { model: true, push: true, computerUse: true, chatApps: true, paired: true },
    member: true,
  },
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

/** A state's fixtures: the desktop frames are the desktop app (Computer Use is its own); a phone is a browser. */
const fixturesOf = (state: State, desktopApp: boolean): WorkbenchFixtures =>
  workbenchFixtures({ ...agentsApi, ...homeFactsApi(state.facts) }, { ...state.base, desktopShell: desktopApp });

const home = phoneShell('/', <Workbench />);

const nativeFixtures = workbenchFixtures({
  ...agentsApi,
  ...homeFactsApi(STATES[2].facts),
  '/api/session': {
    remote: true,
    authenticated: true,
    email: 'max@example.com',
  },
}, STATES[2].base);

const nativeHome = () => (
  <div className="flex h-full flex-col overflow-hidden bg-background text-foreground">
    <MobileBrandHeader nativeAccountAction />
    <div className="min-h-0 flex-1 overflow-hidden px-4">
      <Workbench />
    </div>
  </div>
);

/**
 * The keyboard-up phone states (rule 8 in `HomeLayout`) are drawn without a script: above the
 * keyboard, the page stays at its keyboard-free height and aligns to the bottom. A browser keeps
 * the tab-bar clearance because its keyboard overlays the bar; the native shell removes both the
 * bar and its clearance before revealing the composer in the resized WebView.
 */
const keyboardUp = (nativeShell: boolean) => () => (
  <div className="flex h-full flex-col overflow-hidden bg-background text-foreground">
    <div className="flex min-h-0 flex-1 flex-col justify-end overflow-hidden">
      <div className="flex shrink-0 flex-col">
        <MobileBrandHeader nativeAccountAction={nativeShell} />
        <div
          className={
            nativeShell
              ? 'flex h-[calc(100dvh-3.5rem)] shrink-0 flex-col px-4 [&_[data-home-space=below]]:hidden'
              : 'flex h-[calc(100dvh-3.5rem-var(--mobile-nav-clearance))] shrink-0 flex-col px-4 [&_[data-home-space=below]]:hidden'
          }
        >
          <Workbench />
        </div>
      </div>
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
    status: 'shipped' as const,
    route: '/',
    fixtures: fixturesOf(state, true),
    render: home,
  })),
  ...PHONES.map(({ suffix, width, height }) => ({
    name: `${state.key}-phone${suffix}`,
    title: `首页 · 手机 ${width}×${height} · ${state.title}`,
    width,
    height,
    status: 'shipped' as const,
    route: '/',
    fixtures: fixturesOf(state, false),
    render: home,
  })),
]);

const designFrames = frames([
  ...stateFrames,
  {
    name: 'e-native-phone',
    title: '首页 · iOS App · 账户头像进入账号与实例',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/',
    fixtures: nativeFixtures,
    render: nativeHome,
  },
  {
    name: 'y-browser-keyboard-phone',
    title: '首页 · Safari/PWA 手机键盘弹起：整组不重新居中，底栏被键盘盖住',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/',
    fixtures: fixturesOf(STATES[2], false),
    render: keyboardUp(false),
  },
  {
    name: 'z-keyboard-phone',
    title: '首页 · iOS App 键盘弹起：整组不重新居中，底栏与占位隐藏，输入框完整可见',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/',
    fixtures: nativeFixtures,
    render: keyboardUp(true),
  },
]);

export default designFrames;
