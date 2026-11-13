// Track B (2026-11-13), proposed: the iOS App's account and instance pages as one sheet.
// Plan and decisions: docs/plans/2026-11-13-ios-instance-pages.md. Kit: kit/mobile-instance-sheet.tsx.
// Every state in Chinese and English, light and dark, on the owner's iPhone 17 Pro (402×874, safe
// area 62/34); the long-text states on a 375×667 phone.
import type { ReactNode } from 'react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { frames, type FrameSpec } from '../../frame';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { agentsApi, mainRunningFixtures } from '../../shell/teamFixtures';
import { homeFactsApi, workbenchFixtures } from '../workbench/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import {
  FirstRunPage, InstanceSheet, NamedHome, RuntimeFailed, SheetAccount, SheetLanguage,
  type SheetInstance, type SheetState,
} from '../../kit/mobile-instance-sheet';

const email = 'max@example.com';
const longEmail = 'alexandra.chen+research-and-product-planning@international.example.com';

const personal: SheetInstance = { id: 'personal', name: 'Personal Workbench', host: 'personal-app.avibe.bot', online: true };
const lab: SheetInstance = { id: 'lab', name: 'Lab', host: 'lab-app.avibe.bot', online: true };
const team: SheetInstance = { id: 'team', name: 'Product team', host: 'team-app.avibe.bot', owner: 'Zhou Min', online: true };
const weekend: SheetInstance = { id: 'weekend', name: 'Weekend lab', host: 'weekend-app.avibe.bot', online: false };
/** Most recently opened first (recorded per account on this device); offline ones keep their place. */
const others = [lab, team, weekend];
const longCurrent: SheetInstance = { ...personal, name: 'Personal Workbench for research and product planning' };
const longOthers: SheetInstance[] = [
  { ...team, name: 'Product research team workspace (shared)', owner: 'Alexandra Chen · Product Research' },
  { ...weekend, name: 'Weekend lab on the old Mac mini in the studio' },
];
/** First use has no recency yet: online first, then by name, as the server lists them. */
const firstRun = [lab, personal, team, weekend];

const SMALL = { width: 375, height: 667, safeArea: { top: 20, bottom: 0 } };

function nativeFixtures(lang: 'zh' | 'en', accountEmail = email) {
  return workbenchFixtures({
    ...agentsApi,
    ...homeFactsApi({ model: true, push: false, computerUse: true, chatApps: true, paired: true }),
    '/api/session': { remote: true, authenticated: true, email: accountEmail },
  }, { ...mainRunningFixtures, nativeShell: IOS_APP_CAPABILITIES, copy: copyFor(lang === 'en' ? en : zh) });
}

function both(name: string, title: string, render: () => ReactNode, options: { size?: typeof SMALL; email?: string } = {}): FrameSpec[] {
  const size = options.size ?? IPHONE_17_PRO;
  return (['zh', 'en'] as const).map((lang) => ({
    name: `${name}-${lang}`,
    title: `${title} · ${lang === 'zh' ? '中文' : 'English'}`,
    ...size,
    status: 'proposed',
    themes: ['light', 'dark'],
    route: '/',
    fixtures: nativeFixtures(lang, options.email),
    render: () => <SheetLanguage lang={lang}>{render()}</SheetLanguage>,
  }));
}

const sheet = (state: SheetState) => () => <InstanceSheet state={state} current={personal} others={others} email={email} />;

const designFrames = frames([
  // A · the sheet the avatar opens over the live instance.
  ...both('sheet-default', 'A · 头像打开的 sheet：你在哪、还能去哪、账户在最下面', sheet('default')),
  ...both('sheet-refreshing', 'A · 打开即用缓存显示，后台更新（sheet 里没有下拉刷新）', sheet('refreshing')),
  ...both('sheet-offline-open', 'A · 点离线实例：就地展开说明', sheet('offline-open')),
  ...both('sheet-opening', 'A · 切换中：那一行转圈，其他行暂不可点，关闭仍可用', sheet('opening')),
  ...both('sheet-open-failed', 'A · 切换失败：在那一行报错，人还留在当前实例', sheet('open-failed')),
  ...both('sheet-stale', 'A · 列表没能更新：一条横幅 + 重试，列表照常可用', sheet('stale')),
  ...both('sheet-only-current', 'A · 只有当前这一个实例', sheet('only-current')),
  ...both('sheet-dismissing', 'A · 手势：下拉关闭回到当前实例', sheet('dismissing')),
  ...both('sheet-long', 'A · 长实例名与长邮箱 · 375×667', () => <InstanceSheet state="default" current={longCurrent} others={longOthers} email={longEmail} />, { size: SMALL, email: longEmail }),
  ...both('sheet-long-end', 'A · 长文本 · 滚到底：长邮箱在账户行里完整换行 · 375×667', () => <InstanceSheet state="default" current={longCurrent} others={longOthers} email={longEmail} atEnd />, { size: SMALL, email: longEmail }),
  // Account, pushed inside the sheet.
  ...both('account', '账户：在 sheet 里推入的一页，← 回到实例列表', () => <SheetAccount current={personal} others={others} email={email} />),
  ...both('account-swipe', '手势：从左边缘右滑回到实例列表', () => <SheetAccount current={personal} others={others} email={email} swiping />),
  ...both('account-long', '账户 · 完整长邮箱 · 375×667', () => <SheetAccount current={personal} others={others} email={longEmail} />, { size: SMALL, email: longEmail }),
  // First use: the same rows, as a full page.
  ...both('first-run', '首次使用（还没选过实例）：同样的行，整页', () => <FirstRunPage state="default" instances={firstRun} email={email} />),
  ...both('first-run-pulling', '首次使用 · 下拉刷新（只有整页有）', () => <FirstRunPage state="pulling" instances={firstRun} email={email} />),
  ...both('first-run-error', '首次使用 · 没能载入（没有缓存）', () => <FirstRunPage state="error" instances={[]} email={email} />),
  // Native: the instance failed to load.
  ...both('runtime-failed', '原生 · 实例打不开：重试 / 其他实例（打开同一张 sheet，不先关掉实例）', () => <RuntimeFailed instance={personal} />),
  // B · the header names the instance (needs a Runtime release; older instances keep 「Avibe」 + avatar).
  ...both('b-home', 'B · Home 顶栏写当前实例名，点名字开 sheet，头像只管账户', () => <NamedHome current={personal} />),
  ...both('b-sheet', 'B · 点名字打开的 sheet（没有账户行）', () => <InstanceSheet state="default" current={personal} others={others} email={email} named />),
]);

export default designFrames;
