// The desktop app's native Show window and its share entry (Max, 2026-11-19 19:10: the title-bar
// icon). The window was the system frame and the Show, with no control (`today-*`). Now the title bar
// carries the system share button (`titlebar-idle-*`); a press opens the shipped share popover from
// the content's top right corner (`titlebar-open-*`), with the link, PDF and image of this window's
// Show, and the export notice before a PDF or image (`titlebar-confirm-*`). The Show itself is
// unchanged. Drawn at the native Show window's size (1040 × 720 content under a 28 pt title bar),
// as a native Show window (`nativeShowWindow`). No new copy.
import type { ReactNode } from 'react';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';

import funnelPage from '../../assets/show-page-funnel.svg';
import { frames, type FrameSpec } from '../../frame';
import { session } from '../../shell/fixtures';
import { NativeShowWindow } from '../../kit/desktop-native-show-window';
import { appsFixtures, pinnedDock } from '../apps/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { settingsConfig } from '../settings/fixtures';
import { showPages } from './fixtures';

const page = showPages[0];
const sid = page.session_id;
const title = page.title ?? '';
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

const openShare = 'role=button[name="{t:chat.showPage.share}"]';
const states: { name: string; title: string; render: () => ReactNode; click: string[]; today?: boolean }[] = [
  { name: 'today', title: '原生 Show 窗口 · 之前：系统边框和 Show，没有任何控件', render: () => <NativeShowWindow title={title} today />, click: [], today: true },
  { name: 'titlebar-idle', title: '原生 Show 窗口 · 标题栏右侧系统分享按钮（未点开）', render: () => <NativeShowWindow title={title} />, click: [] },
  { name: 'titlebar-open', title: '原生 Show 窗口 · 点开：同一个分享弹层，从内容右上角弹出', render: () => <NativeShowWindow title={title} />, click: [openShare] },
  {
    name: 'titlebar-confirm',
    title: '原生 Show 窗口 · 导出前：可见范围与副本提示',
    render: () => <NativeShowWindow title={title} />,
    click: [openShare, 'role=button[name="{t:chat.showPage.sharePdf}"]'],
  },
];

const WIDTH = 1040;
const HEIGHT = 748;

const designFrames = frames(states.flatMap(({ name, title: frameTitle, render, click }): FrameSpec[] =>
  (['zh', 'en'] as const).map((lang) => ({
    name: `${name}-${lang}`,
    title: `${frameTitle} · ${lang === 'zh' ? '中文' : 'English'}`,
    width: WIDTH,
    height: HEIGHT,
    status: 'shipped',
    themes: ['light', 'dark'],
    route: `/apps/show/${sid}?standalone=1`,
    fixtures: appsFixtures(api(lang), {
      nativeShowWindow: true,
      copy: copyFor(lang === 'en' ? en : zh),
    }),
    wait: 800,
    ...(click.length ? { click } : {}),
    render,
  }))));

export default designFrames;
