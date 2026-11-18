// The iPhone App shares the Show it displays as its link, a PDF, or an image of what is on screen
// (docs/plans/2026-11-18-ios-show-outbound-sharing.md). The shipped Show Page route and its share
// control, on the owner's iPhone 17 Pro, in Chinese and English, light and dark.
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { ShowPageRoute } from '@/components/apps/ShowPageRoute';

import funnelPage from '../../assets/show-page-funnel.svg';
import { frames, type FrameSpec } from '../../frame';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO, session } from '../../shell/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { showPages } from './fixtures';
import { settingsConfig } from '../settings/fixtures';

const page = showPages[0];
const sid = page.session_id;
const route = `/apps/show/${sid}`;
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
const api = {
  [`/api/sessions/${sid}`]: session(sid, 'proj_avibe', page.title ?? ''),
  [`/show/${sid}/`]: funnelPage,
  [`/api/show-pages/${sid}`]: page,
  [`/api/show-pages/${sid}/access`]: access,
  [`/api/show-pages/${sid}/access-settings/read`]: {
    show_access: { page_id: sid, access_mode: page.access_mode, share_id: page.share_id, revision: page.access_revision, normalized_emails: [], entries: [] },
  },
};

/** The App that shares Shows (`c1.share_draft`), and one released before it. */
const SHARING_APP = { ...IOS_APP_CAPABILITIES, 'c1.share_draft': 1 };

const openShare = 'role=button[name="{t:chat.showPage.share}"]';
const states: { name: string; title: string; shell: Record<string, number>; click: string[] }[] = [
  { name: 'menu', title: '分享 · 链接、PDF 与图片', shell: SHARING_APP, click: [openShare] },
  {
    name: 'confirm',
    title: '导出前 · 可见范围与副本提示',
    shell: SHARING_APP,
    click: [openShare, 'role=button[name="{t:chat.showPage.sharePdf}"]'],
  },
  { name: 'update', title: '旧版 App · 仅能分享链接', shell: IOS_APP_CAPABILITIES, click: [openShare] },
];

const designFrames = frames(states.flatMap(({ name, title, shell, click }): FrameSpec[] =>
  (['zh', 'en'] as const).map((lang) => ({
    name: `${name}-${lang}`,
    title: `${title} · ${lang === 'zh' ? '中文' : 'English'}`,
    ...IPHONE_17_PRO,
    status: 'shipped',
    themes: ['light', 'dark'],
    route,
    // Locale-specific ids carry their locale into the shipped strings in either canvas language.
    fixtures: workbenchFixtures({ ...api, '/api/config': { ...settingsConfig, language: lang } },
      { nativeShell: shell, copy: copyFor(lang === 'en' ? en : zh) }),
    wait: 800,
    click,
    render: phoneShell('/apps/show/:sessionId', <ShowPageRoute />),
  }))));

export default designFrames;
