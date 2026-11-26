// Proposal (Max, 2026-11-26 09:37): in the desktop app, signed in, the instance button at the foot
// of the sidebar opens a menu above it, as Claude's account menu does: the signed-in email and the
// instance this window shows, neither a control, a line, then Switch instance and Sign out on this
// device (destructive). Two readings of the instance line: A, its status dot and name; B, the
// instance list's row in small, its icon, dot, name and domain. Today the button opens the Instances
// page straight away. The row, the version beside it, Settings and the signed-out row are
// chrome/bottom-row's and do not change. Each frame is the complete window.
import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { AppShell, AppShellFooter } from '@/components/AppShell';
import { VersionBadge } from '@/components/VersionBadge';
import { Workbench } from '@/components/Workbench';
import { DesktopAccountRowView } from '@/desktop/DesktopAccountRowView';
import { InstanceVersionChipView } from '@/desktop/InstanceVersionChip';
import type { DesktopClientProjection } from '@/desktop/bridge';

import { frames, type FrameSpec } from '../../frame';
import { DesktopSidebarAccountMenu, type AccountMenuInstanceStyle } from '../../kit/desktop-sidebar-account-menu';
import { agentsApi } from '../agents/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { homeFactsApi, workbenchFixtures } from '../workbench/fixtures';

type Lang = 'zh' | 'en';
const LOCAL = '小麦的 Mac';
const SLUG = 'max';
const LOCAL_HOST = 'max-app.avibe.bot';
const REMOTE = 'Studio';
const REMOTE_HOST = 'studio-app.avibe.bot';
const EMAIL = 'max@example.test';
const LONG = 'research-and-product-planning-workstation';
const LONG_EMAIL = 'maximilian.product-research@example-company.test';

const fixturesFor = (lang: Lang) => workbenchFixtures({
  ...agentsApi(),
  ...homeFactsApi({ model: true, push: true, computerUse: true, chatApps: true, paired: true }),
  '/api/session': { remote: false },
  '/api/version': { current: '3.2.7', latest: '3.2.7', has_update: false, error: null },
}, { copy: copyFor(lang === 'en' ? en : zh) });

type Link = 'local' | 'remote' | 'offline' | 'switching';
const STUDIO = { kind: 'remote', name: REMOTE, instance_id: 'studio', canonical_origin: `https://${REMOTE_HOST}` } as const;

const projection = (link: Link, name?: string, email = EMAIL): DesktopClientProjection => ({
  version: 1, revision: 1, generation: 1,
  account: { status: 'signed_in', identity: { id: 'fixture', email } },
  connection: link === 'switching'
    ? { status: 'switching', committed: { kind: 'local', name: LOCAL, instance_id: null }, pending: STUDIO }
    : link === 'local'
      ? { status: 'local', committed: { kind: 'local', name: LOCAL, instance_id: null }, pending: null }
      : { status: link === 'offline' ? 'remote_unreachable' : 'remote_connected', committed: { ...STUDIO, name: name ?? REMOTE }, pending: null },
  local: { name: LOCAL, slug: link === 'local' || link === 'switching' ? name ?? SLUG : SLUG, remote_access: 'on', remote_access_url: `https://${LOCAL_HOST}` },
  desktop: { version: '3.2.7', update: { phase: 'current', latest: null, channel: 'stable', skippedVersion: null, interactive: false } },
  remote_auxiliary: link === 'local' || link === 'switching' ? 'local' : 'disabled_incomplete',
});

const row = (link: Link, name?: string, email?: string) => {
  const remote = link === 'remote' || link === 'offline';
  return {
    state: projection(link, name, email),
    localUpdater: remote ? null : <VersionBadge openUpward size="row" />,
    ...(remote ? { instanceVersion: <InstanceVersionChipView name={name ?? REMOTE} version="3.2.7" attention={false} size="row" /> } : {}),
  };
};
const hostOf = (link: Link) => (link === 'remote' || link === 'offline' ? REMOTE_HOST : LOCAL_HOST);

const shell = (footer: ReactNode) => () => (
  <Routes>
    <Route element={<AppShell footer={({ tools }) => <AppShellFooter tools={tools} divided>{footer}</AppShellFooter>} />}>
      <Route path="/" element={<Workbench />} />
    </Route>
  </Routes>
);
const menu = (style: AccountMenuInstanceStyle, link: Link, name?: string, email?: string) =>
  shell(<DesktopSidebarAccountMenu row={row(link, name, email)} instanceStyle={style} host={hostOf(link)} />);

type Shot = { name: string; title: string; render: () => ReactNode; hover?: string; width?: number; langs?: Lang[] };
const LINK_TITLE: Record<Exclude<Link, 'remote'>, string> = { local: '在线（绿点）', offline: '连接断开（灰点）', switching: '正在连接（金点）' };

const optionShots = (style: AccountMenuInstanceStyle): Shot[] => {
  const label = style === 'A' ? '方案 A · 色点＋实例名' : '方案 B · 图标＋色点＋实例名＋域名';
  const key = style.toLowerCase();
  return [
    { name: `${key}-online`, title: `${label} · ${LINK_TITLE.local}`, render: menu(style, 'local'), langs: ['zh', 'en'] },
    { name: `${key}-offline`, title: `${label} · 另一台电脑 · ${LINK_TITLE.offline}`, render: menu(style, 'offline') },
    { name: `${key}-switching`, title: `${label} · ${LINK_TITLE.switching}`, render: menu(style, 'switching') },
    { name: `${key}-hover`, title: `${label} · 指针悬停在「切换实例」上`, render: menu(style, 'local'), hover: '[data-menu-switch] button' },
    { name: `${key}-long-narrow`, title: `${label} · 长实例名＋长邮箱 · 窄窗口 880`, render: menu(style, 'local', LONG, LONG_EMAIL), width: 880 },
  ];
};

const shots: Shot[] = [
  {
    name: 'closed', title: '菜单展开前 · 侧栏底部现状（本机实例 max，在线）',
    render: shell(<DesktopAccountRowView {...row('local')} onPresent={() => {}} />),
  },
  ...optionShots('A'),
  ...optionShots('B'),
  { name: 'b-remote', title: '方案 B · 另一台电脑 · 在线（图标为地球，域名为那台的）', render: menu('B', 'remote') },
  { name: 'b-hover-sign-out', title: '方案 B · 指针悬停在「在此设备退出」上', render: menu('B', 'local'), hover: '[data-menu-sign-out] button' },
];

const designFrames = frames(shots.flatMap((shot) => (shot.langs ?? ['zh']).map((lang): FrameSpec => ({
  name: `${shot.name}-${lang}`,
  title: `${shot.title} · ${lang}`,
  width: shot.width ?? 1200,
  height: 800,
  status: shot.name === 'closed' ? 'shipped' : 'proposed',
  route: '/',
  fixtures: fixturesFor(lang),
  render: shot.render,
  ...(shot.hover ? { hover: shot.hover } : {}),
}))));

export default designFrames;
