// Approved by the owner on 2026-11-14 13:23 (Asia/Shanghai): the iOS App's account and instance pages as one
// sheet. Variant A (the avatar opens it) shipped first; variant B (the instance name in Home's header opens the
// instances, every account entry opens Account) ships with Runtime 3.2.7.
// Plan and decisions: docs/plans/2026-11-13-ios-instance-pages.md. The pages are the shipped ones (ui/src/mobile);
// kit/mobile-instance-sheet.tsx draws only what the web cannot: the native sheet, gestures, and the native
// failure screen.
// Every state in Chinese and English, light and dark, on the owner's iPhone 17 Pro (402×874, safe
// area 62/34); the long-text states on a 375×667 phone.
import type { ReactNode } from 'react';
import { ArrowDown, ArrowRight } from 'lucide-react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { frames, type FrameSpec } from '../../frame';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { agentsApi, mainRunningFixtures } from '../../shell/teamFixtures';
import { homeFactsApi, workbenchFixtures } from '../workbench/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { phoneShell } from '../workbench/fixtures';
import { Workbench } from '@/components/Workbench';
import { AccountDataSheetPage, AccountSheetPage, InstanceSheetPage, InstancesPage } from '@/mobile/MobileOnboarding';
import type { MobileInstance, MobileSessionState } from '@/mobile/bridge';
import { GestureNote, NativeSheet, RuntimeFailed, SheetLanguage, SheetPanel, Underneath } from '../../kit/mobile-instance-sheet';

const email = 'max@example.com';
const longEmail = 'alexandra.chen+research-and-product-planning@international.example.com';

/** An instance as the App's session lists it. */
const instance = (id: string, name: string, host: string, extra: Partial<MobileInstance> = {}): MobileInstance => ({
  id, name, host, url: `https://${host}`, relationship: 'owner', owner_email: null, available: true, ...extra,
});
const personal = instance('personal', 'Personal Workbench', 'personal-app.avibe.bot');
const lab = instance('lab', 'Lab', 'lab-app.avibe.bot');
const team = instance('team', 'Product team', 'team-app.avibe.bot', { relationship: 'shared', owner_email: 'Zhou Min' });
const weekend = instance('weekend', 'Weekend lab', 'weekend-app.avibe.bot', { available: false });
/** Most recently opened first (recorded per account on this device); offline ones keep their place. */
const others = [lab, team, weekend];
const longCurrent = { ...personal, name: 'Personal Workbench for research and product planning' };
const longOthers = [
  { ...team, name: 'Product research team workspace (shared)', owner_email: 'Alexandra Chen · Product Research' },
  { ...weekend, name: 'Weekend lab on the old Mac mini in the studio' },
];
/** First use has no recency yet: online first, then by name, as the server lists them. */
const firstRun = [lab, personal, team, weekend];

const SMALL = { width: 375, height: 667, safeArea: { top: 20, bottom: 0 } };

/** Variant B's shell: it names the selected instance (`instancePresentation`, `app.info`). */
const B_SHELL = { ...IOS_APP_CAPABILITIES, instancePresentation: 1 };

function nativeFixtures(lang: 'zh' | 'en', accountEmail = email, b = false) {
  return workbenchFixtures({
    ...agentsApi,
    ...homeFactsApi({ model: true, push: false, computerUse: true, chatApps: true, paired: true }),
    '/api/session': { remote: true, authenticated: true, email: accountEmail },
  }, {
    ...mainRunningFixtures,
    nativeShell: b ? B_SHELL : IOS_APP_CAPABILITIES,
    ...(b ? { nativeInstanceName: personal.name } : {}),
    copy: copyFor(lang === 'en' ? en : zh),
  });
}

type SignedIn = Extract<MobileSessionState, { status: 'signedIn' }>;
const session = (instances: MobileInstance[], selected: string | null = personal.id, accountEmail = email): SignedIn => ({
  status: 'signedIn',
  account: { id: 'account', email: accountEmail },
  selected_instance_id: selected,
  instances,
});
const bSession = session([personal, ...others]);
const noop = () => {};
type Rows = Parameters<typeof InstanceSheetPage>[0]['rows'];
const rows = (overrides: Partial<Rows> = {}): Rows => ({ openingId: null, failure: null, expandedId: null, onOpen: noop, ...overrides });

function both(name: string, title: string, render: () => ReactNode, options: { size?: typeof SMALL; email?: string; b?: boolean; status?: FrameSpec['status'] } = {}): FrameSpec[] {
  const size = options.size ?? IPHONE_17_PRO;
  return (['zh', 'en'] as const).map((lang) => ({
    name: `${name}-${lang}`,
    title: `${title} · ${lang === 'zh' ? '中文' : 'English'}`,
    ...size,
    status: options.status ?? 'approved',
    themes: ['light', 'dark'],
    route: '/',
    fixtures: nativeFixtures(lang, options.email, options.b),
    render: () => <SheetLanguage lang={lang}>{render()}</SheetLanguage>,
  }));
}

type SheetProps = Parameters<typeof InstanceSheetPage>[0];
/** A's sheet (the avatar opens it): the shipped page with the account row. */
const sheetPage = (props: Partial<SheetProps> = {}) => (
  <InstanceSheetPage
    session={bSession}
    locked={false}
    refreshing={false}
    stale={false}
    rows={rows()}
    onClose={noop}
    onRefresh={noop}
    onAdd={noop}
    onAccount={noop}
    {...props}
  />
);
const sheet = (props: Partial<SheetProps> = {}) => () => <NativeSheet>{sheetPage(props)}</NativeSheet>;

type AccountProps = Parameters<typeof AccountSheetPage>[0];
/** Account in the sheet: pushed from the list (A, with Back), or its root with Close (B). */
const accountPage = (props: Partial<AccountProps> = {}) => (
  <AccountSheetPage
    session={bSession}
    busy={false}
    locked={false}
    errorKey={null}
    licenses
    onBack={noop}
    onSwitch={noop}
    onLogout={noop}
    onLicenses={noop}
    onAccountData={noop}
    {...props}
  />
);

/** First use (nothing selected yet): the shipped full-screen list. */
const firstRunPage = (props: Partial<Parameters<typeof InstancesPage>[0]> = {}) => (
  <InstancesPage
    session={session(firstRun, null)}
    busy={null}
    locked={false}
    errorKey={null}
    rows={rows()}
    onRefresh={noop}
    onAccount={noop}
    onAdd={noop}
    onReopen={noop}
    {...props}
  />
);

/**
 * The full page with an instance selected but none open (say it failed to load): the sheet's list
 * in a page. The current instance opens again from here; there is nothing behind it to close to.
 */
const switchingPage = (props: Partial<Parameters<typeof InstancesPage>[0]> = {}) => firstRunPage({ session: bSession, ...props });

const designFrames = frames([
  // A · the sheet the avatar opens over the live instance.
  ...both('sheet-default', 'A · 头像打开的 sheet：你在哪、还能去哪、账号在最下面', sheet(), { status: 'shipped' }),
  ...both('sheet-refreshing', 'A · 打开即用缓存显示，后台更新（sheet 里没有下拉刷新）', sheet({ refreshing: true }), { status: 'shipped' }),
  ...both('sheet-offline-open', 'A · 点离线实例：就地展开说明', sheet({ rows: rows({ expandedId: weekend.id }) }), { status: 'shipped' }),
  ...both('sheet-opening', 'A · 切换中：那一行转圈，其他行暂不可点，关闭仍可用', sheet({ locked: true, rows: rows({ openingId: lab.id }) }), { status: 'shipped' }),
  ...both('sheet-open-failed', 'A · 切换失败：在那一行报错，人还留在当前实例', sheet({ rows: rows({ failure: { id: lab.id, reason: 'mobileApp.sheet.openFailed' } }) }), { status: 'shipped' }),
  ...both('sheet-stale', 'A · 列表没能更新：一条横幅 + 重试，列表照常可用', sheet({ stale: true }), { status: 'shipped' }),
  ...both('sheet-only-current', 'A · 只有当前这一个实例', sheet({ session: session([personal]) }), { status: 'shipped' }),
  ...both('sheet-dismissing', 'A · 手势：下拉关闭回到当前实例', () => (
    <Underneath>
      <SheetPanel className="translate-y-1/4">{sheetPage()}</SheetPanel>
      <GestureNote
        className="top-[calc(env(safe-area-inset-top)+6rem)] left-1/2 -translate-x-1/2"
        icon={<ArrowDown className="size-4" />}
        zh={`下拉关闭，回到 ${personal.name}`}
        en={`Drag down to return to ${personal.name}`}
      />
    </Underneath>
  )),
  ...both('sheet-long', 'A · 长实例名与长邮箱 · 375×667', sheet({ session: session([longCurrent, ...longOthers], personal.id, longEmail) }), { size: SMALL, email: longEmail, status: 'shipped' }),
  ...both('sheet-long-end', 'A · 长文本 · 滚到底：长邮箱在账号行里完整换行 · 375×667', () => (
    <NativeSheet atEnd>{sheetPage({ session: session([longCurrent, ...longOthers], personal.id, longEmail) })}</NativeSheet>
  ), { size: SMALL, email: longEmail, status: 'shipped' }),
  // Account, pushed inside the sheet.
  ...both('account', '账号：在 sheet 里推入的一页，← 回到实例列表', () => <NativeSheet>{accountPage()}</NativeSheet>, { status: 'shipped' }),
  ...both('account-swipe', '手势：从左边缘右滑回到实例列表', () => (
    <Underneath>
      <SheetPanel>{sheetPage()}</SheetPanel>
      <SheetPanel className="translate-x-1/3 shadow-elevation-3">{accountPage()}</SheetPanel>
      <GestureNote
        className="bottom-[calc(env(safe-area-inset-bottom)+5rem)] left-4"
        icon={<ArrowRight className="size-4" />}
        zh="从左边缘右滑，回到实例列表"
        en="Swipe from the left edge to go back"
      />
    </Underneath>
  )),
  // Account and data, one page below Account (owner, 2026-11-17): the privacy policy, and deleting
  // the account as one plain line at the very bottom.
  ...both('account-data', '账号与数据：从账号页推入；隐私政策，最底部一行灰字「删除账号」', () => (
    <NativeSheet><AccountDataSheetPage locked={false} onBack={noop} onPrivacy={noop} onDelete={noop} /></NativeSheet>
  )),
  ...both('account-long', '账号 · 完整长邮箱 · 375×667', () => <NativeSheet>{accountPage({ session: session([personal, ...others], personal.id, longEmail) })}</NativeSheet>, { size: SMALL, email: longEmail, status: 'shipped' }),
  // First use: the same rows, as a full page.
  ...both('first-run', '首次使用（还没选过实例）：同样的行，整页', () => firstRunPage(), { status: 'shipped' }),
  ...both('first-run-pulling', '首次使用 · 下拉刷新（只有整页有）', () => (
    <div className="relative h-full">
      {firstRunPage()}
      <GestureNote
        className="top-[calc(env(safe-area-inset-top)+3.5rem)] left-1/2 -translate-x-1/2"
        icon={<ArrowDown className="size-4" />}
        zh="在顶部下拉，刷新列表"
        en="Pull down at the top to refresh"
      />
    </div>
  )),
  // The full page with a selection: the same list as the sheet, in a page.
  ...both('page-switching', '整页 · 有选中实例（例如实例没能打开后）：和 sheet 同一个列表；点当前实例重新打开', () => switchingPage(), { status: 'shipped' }),
  ...both('page-switching-stale', '整页 · 刷新失败：横幅 + 重试，列表照常可用', () => switchingPage({ errorKey: 'mobileApp.errors.network' }), { status: 'shipped' }),
  ...both('first-run-error', '首次使用 · 没能载入（没有缓存）', () => firstRunPage({ session: session([], null), errorKey: 'mobileApp.errors.network' }), { status: 'shipped' }),
  // Native: the instance failed to load.
  ...both('runtime-failed', '原生 · 实例打不开：重试 / 其他实例（打开同一张 sheet，不先关掉实例）', () => <RuntimeFailed instance={personal} />),
  // B · the header names the instance (needs a Runtime release; older instances keep 「Avibe」 + avatar).
  ...both('b-home', 'B · Home 顶栏写当前实例名，点名字开实例列表，头像直接打开账号', phoneShell('/', <Workbench />), { b: true, status: 'shipped' }),
  ...both('b-sheet', 'B · 点名字打开的 sheet（没有账号行）', sheet({ onAccount: undefined }), { b: true, status: 'shipped' }),
  // B's account entries open the same sheet on the Account page (#sheet-account): ✕ returns to the
  // instance, there is no Back, and the licenses still push inside it. Compare A's `account`.
  ...both('b-account', 'B · 账号入口打开的 sheet 直接是账号页：右上角 ✕ 回到实例，没有返回', () => <NativeSheet>{accountPage({ onClose: noop })}</NativeSheet>, { b: true, status: 'shipped' }),
]);

export default designFrames;
