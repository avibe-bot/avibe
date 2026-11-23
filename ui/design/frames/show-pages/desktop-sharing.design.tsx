// The desktop app shares a Show's link from the Workbench through the macOS share menu
// (NSSharingServicePicker, docs/plans/2026-11-19-desktop-show-outbound-sharing.md), with the same
// C1 v1 contract and share control as the iPhone (`show-pages/ios-sharing/*`); the link keeps its
// access. The Workbench has no trusted tie to the pixels of a Show it embeds, so PDF and image are
// offered only in the native Show window (`show-pages/desktop-native-entry/*`). No new copy.
//
// Drawn with the shipped Show Page window and share control, in the desktop app (`desktopShell`) with
// its Show share command (`desktopShowShare`). `legacy` is a desktop app without it (an older
// version or macOS, or a page another computer serves): copy the link, no share button.
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';

import funnelPage from '../../assets/show-page-funnel.svg';
import { frames, type FrameSpec } from '../../frame';
import { session } from '../../shell/fixtures';
import { appsFixtures, pinnedDock, soloWindow, windowOnly } from '../apps/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { settingsConfig } from '../settings/fixtures';
import { showPages } from './fixtures';

const page = showPages[0];
const sid = page.session_id;
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
  [`/api/sessions/${sid}`]: session(sid, 'proj_avibe', page.title ?? ''),
  [`/show/${sid}/`]: funnelPage,
  [`/api/show-pages/${sid}`]: page,
  [`/api/show-pages/${sid}/access`]: access,
  [`/api/show-pages/${sid}/access-settings/read`]: {
    show_access: { page_id: sid, access_mode: page.access_mode, share_id: page.share_id, revision: page.access_revision, normalized_emails: [], entries: [] },
  },
});

const WIDTH = 1100;
const HEIGHT = 760;
const layout = soloWindow('showpage', WIDTH, HEIGHT, { title: page.title ?? '', params: { sessionId: sid, title: page.title } });

const openShare = 'role=button[name="{t:chat.showPage.share}"]';
const states: { name: string; title: string; sharing: boolean; click: string[] }[] = [
  { name: 'menu', title: '桌面 Workbench · 只分享链接（PDF/图片在原生 Show 窗口）', sharing: true, click: [openShare] },
  { name: 'legacy', title: '桌面 · 没有分享能力（今天的桌面版、旧版或另一台电脑的页面）：只复制链接', sharing: false, click: [openShare] },
];

const designFrames = frames(states.flatMap(({ name, title, sharing, click }): FrameSpec[] =>
  (['zh', 'en'] as const).map((lang) => ({
    name: `${name}-${lang}`,
    title: `${title} · ${lang === 'zh' ? '中文' : 'English'}`,
    width: WIDTH,
    height: HEIGHT,
    status: 'shipped',
    themes: ['light', 'dark'],
    fixtures: appsFixtures(api(lang), {
      storage: layout,
      desktopShell: true,
      copy: copyFor(lang === 'en' ? en : zh),
      desktopShowShare: sharing,
    }),
    wait: 800,
    click,
    render: windowOnly,
  }))));

export default designFrames;
