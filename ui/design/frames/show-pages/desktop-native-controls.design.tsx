// The native Show window's remaining controls (Max, 2026-11-20 11:22: "合完出设计图"). The window
// carries the share button today (show-pages/desktop-native-entry/titlebar-idle); the Workbench's
// own Show window carries three more — annotate with its mode picker, open the session's chat, and
// open in the browser (apps/show-page-window/window). These frames place those three.
//
// A, recommended (`group-*`): one group in the title bar's accessory, in the Workbench window's own
// order — 标注 · 打开聊天 · 分享 · 在浏览器打开 — 28 pt buttons 4 pt apart, the group 9 pt from the
// window's right edge. Each popover opens under its own button. Nothing collapses: the window
// cannot be narrower than 720 pt, where the group leaves the title about 490 pt (`group-min`).
//
// B, the alternative (`overflow-*`): the share button keeps the title bar to itself and the other
// three move behind `•••`. One native button instead of three, at the cost of a second click, a
// second level for the annotation mode, and an annotation that is on with nothing in the title bar
// saying so.
//
// Proposed, and not shipped: the native title bar and its buttons are a stand-in
// (`kit/desktop-native-show-controls.tsx`) for AppKit's title bar and `macos_show_titlebar.rs`,
// which no web component renders. The annotate popover, the share popover and the Show below them
// are the shipped components. No new copy: every label is one the Workbench window already reads.
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

interface State {
  name: string;
  title: string;
  render: () => ReactNode;
  click?: string[];
  width?: number;
  height?: number;
}

const states: State[] = [
  {
    name: 'group-idle',
    title: 'A 推荐 · 标题栏一组四个控件（标注 · 打开聊天 · 分享 · 在浏览器打开）',
    render: () => <NativeShowControlsWindow title={title} sessionId={sid} />,
  },
  {
    name: 'group-annotate',
    title: 'A 推荐 · 标注已开启，模式选择弹层在自己的按钮下方',
    render: () => (
      <NativeShowControlsWindow
        title={title}
        sessionId={sid}
        annotation={{ enabled: true, mode: 'smart', available: true }}
      />
    ),
    click: ['role=button[name="{t:chat.showPage.annotate.toggle}"]'],
  },
  {
    name: 'group-share',
    title: 'A 推荐 · 分享弹层在分享按钮下方（与今天同一个弹层）',
    render: () => <NativeShowControlsWindow title={title} sessionId={sid} />,
    click: ['role=button[name="{t:chat.showPage.share}"]'],
  },
  {
    name: 'group-min',
    title: 'A 推荐 · 最窄窗口 720pt，长标题：控件不收起，标题在控件组前截断',
    render: () => <NativeShowControlsWindow title={longTitle} sessionId={sid} titleDraw="drawn" />,
    width: MIN_WIDTH,
    height: MIN_HEIGHT,
  },
  {
    name: 'overflow-idle',
    title: 'B 备选 · 标题栏只留分享，其余三个收进「更多」',
    render: () => <NativeShowControlsWindow title={title} sessionId={sid} variant="overflow" />,
  },
  {
    name: 'overflow-menu',
    title: 'B 备选 · 「更多」菜单展开：标注（需二级选模式）· 打开聊天 · 在浏览器打开',
    render: () => <NativeShowControlsWindow title={title} sessionId={sid} variant="overflow" />,
    click: ['role=button[name="{t:nav.more}"]'],
  },
];

const designFrames = frames(states.flatMap(({ name, title: frameTitle, render, click, width, height }): FrameSpec[] =>
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
    render,
  }))));

export default designFrames;
