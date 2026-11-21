// The native Show window's remaining controls (Max, 2026-11-20 11:22: "合完出设计图"). The window
// carries the share button today (show-pages/desktop-native-entry/titlebar-idle); the Workbench's
// own Show window carries three more — annotate with its mode picker, open the session's chat, and
// open in the browser (apps/show-page-window/window). These frames place those three.
//
// A, which Max chose on 2026-11-21 09:36 (`group-*`): one group in the title bar's accessory, in the
// Workbench window's own order — 标注 · 打开聊天 · 分享 · 在浏览器打开 — pressed anywhere in a 28 pt
// square, 4 pt apart, the group 9 pt from the window's right edge. Each popover opens under its own
// button. Nothing collapses: the window cannot be narrower than 720 pt, where the group leaves the
// title about 490 pt (`group-min`).
//
// Its symbol is 14 pt and its hover wash a 20 pt square inside the 28 pt press target, so the wash
// keeps 4 pt clear of the title bar's top and bottom (Max, 09:36 and 09:47, from `group-hover`).
// The shipped share button's 15 pt symbol joins this group and drops to 14 with the rest.
//
// B, the alternative Max did not take (`overflow-*`): the share button keeps the title bar to itself
// and the other three move behind `•••`. One native button instead of three, at the cost of a second
// click and a second level for the annotation mode.
//
// Proposed, and not shipped: the native title bar and its buttons are a stand-in
// (`kit/desktop-native-show-controls.tsx`) for AppKit's title bar and `macos_show_titlebar.rs`,
// which no web component renders. The annotate popover, the share popover and the Show below them
// are the shipped components. One new string, `apps.window.openInBrowser`, which a native window
// needs because it has no tabs; every other label is one the Workbench window already reads.
import type { ReactNode } from 'react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';

import funnelPage from '../../assets/show-page-funnel.svg';
import { frames, type FrameSpec } from '../../frame';
import { session } from '../../shell/fixtures';
import { NativeShowControlsWindow } from '../../kit/desktop-native-show-controls';
import { appsFixtures, pinnedDock } from '../apps/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { settingsConfig } from '../settings/fixtures';
import { showPages } from './fixtures';

const page = showPages[0];
const sid = page.session_id;
const title = page.title ?? '';
// A title longer than the bar can hold, to show where it stops.
const longTitle = `${title} · 市场与增长团队月度复盘，含各渠道拆分、ROI 对比与下季度预算建议（11 月 21 日更新）`;
const access = {
  ok: true,
  mode: 'personal',
  ownership_status: 'unmanaged',
  instance_id: null,
  organization_id: null,
  policy_organization_id: null,
  access_level: 'owner',
  group_ids: [],
  policy_revision: null,
  last_applied_control_plane_revision: null,
  can_use: true,
  can_manage: true,
  can_publish_public: true,
};
const api = (lang: 'zh' | 'en') => ({
  '/api/config': { ...settingsConfig, language: lang },
  '/api/dock': { ok: true, dock: pinnedDock() },
  [`/api/sessions/${sid}`]: session(sid, 'proj_avibe', title),
  [`/show/${sid}/`]: funnelPage,
  [`/api/show-pages/${sid}`]: page,
  [`/api/show-pages/${sid}/access`]: access,
  [`/api/show-pages/${sid}/access-settings/read`]: {
    show_access: { page_id: sid, access_mode: page.access_mode, share_id: page.share_id, revision: page.access_revision, normalized_emails: [], entries: [] },
  },
});

const WIDTH = 1040;
const HEIGHT = 748;
/** The window's narrowest: `app_windows.rs` holds a Show window at 720 × 480 and the title bar is 28. */
const MIN_WIDTH = 720;
const MIN_HEIGHT = 508;

/** Proposed copy for `apps.window.openInBrowser`: a native window has no tabs, so the Workbench's
 *  "open in new tab" label cannot stand in for it. It moves to `src/i18n` when the design ships. */
const browserLabel = (lang: 'zh' | 'en') => (lang === 'zh' ? '在浏览器打开' : 'Open in Browser');

interface State {
  name: string;
  title: string;
  render: (lang: 'zh' | 'en') => ReactNode;
  hover?: string;
  click?: string[];
  width?: number;
  height?: number;
}

const states: State[] = [
  {
    name: 'group-idle',
    title: 'A · 标题栏一组四个控件（标注 · 打开聊天 · 分享 · 在浏览器打开）',
    render: (lang) => <NativeShowControlsWindow title={title} sessionId={sid} browserLabel={browserLabel(lang)} />,
  },
  {
    name: 'group-hover',
    title: 'A · 鼠标移到「打开聊天」上：悬停底色',
    render: (lang) => <NativeShowControlsWindow title={title} sessionId={sid} browserLabel={browserLabel(lang)} />,
    hover: '[data-titlebar-control="chat"]',
  },
  {
    name: 'group-annotate',
    title: 'A · 点开标注：同一个模式选择弹层，在自己的按钮下方（标注已开，所以有「关闭标注」）',
    render: (lang) => (
      <NativeShowControlsWindow
        title={title}
        sessionId={sid}
        browserLabel={browserLabel(lang)}
        annotation={{ enabled: true, mode: 'smart', available: true }}
      />
    ),
    click: ['role=button[name="{t:chat.showPage.annotate.toggle}"]'],
  },
  {
    name: 'group-share',
    title: 'A · 分享弹层在分享按钮下方（与今天同一个弹层）',
    render: (lang) => <NativeShowControlsWindow title={title} sessionId={sid} browserLabel={browserLabel(lang)} />,
    click: ['role=button[name="{t:chat.showPage.share}"]'],
  },
  {
    name: 'group-min',
    title: 'A · 最窄窗口 720pt，长标题：控件不收起，标题在控件组前截断',
    render: (lang) => <NativeShowControlsWindow title={longTitle} sessionId={sid} browserLabel={browserLabel(lang)} titleDraw="drawn" />,
    width: MIN_WIDTH,
    height: MIN_HEIGHT,
  },
  {
    name: 'overflow-idle',
    title: 'B（未采用）· 标题栏只留分享，其余三个收进「更多」',
    render: (lang) => <NativeShowControlsWindow title={title} sessionId={sid} browserLabel={browserLabel(lang)} variant="overflow" />,
  },
  {
    name: 'overflow-menu',
    title: 'B（未采用）· 「更多」菜单展开：标注（需二级选模式）· 打开聊天 · 在浏览器打开',
    render: (lang) => <NativeShowControlsWindow title={title} sessionId={sid} browserLabel={browserLabel(lang)} variant="overflow" />,
    click: ['role=button[name="{t:nav.more}"]'],
  },
];

const designFrames = frames(states.flatMap(({ name, title: frameTitle, render, click, hover, width, height }): FrameSpec[] =>
  (['zh', 'en'] as const).map((lang) => ({
    name: `${name}-${lang}`,
    title: `${frameTitle} · ${lang === 'zh' ? '中文' : 'English'}`,
    width: width ?? WIDTH,
    height: height ?? HEIGHT,
    status: 'proposed',
    themes: ['light', 'dark'],
    route: `/apps/show/${sid}?standalone=1`,
    fixtures: appsFixtures(api(lang), {
      nativeShowWindow: true,
      copy: copyFor(lang === 'en' ? en : zh),
    }),
    wait: 800,
    ...(click ? { click } : {}),
    ...(hover ? { hover } : {}),
    render: () => render(lang),
  }))));

export default designFrames;
