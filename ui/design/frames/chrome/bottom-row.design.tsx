// The sidebar's bottom row on every surface, as shipped: the Web, the desktop app
// on this computer, and the desktop app showing another computer. One component
// (`SidebarStatusRow`): the status on the left, the version chip on the right.
// The version beside a name is that instance's own: on another computer's instance
// it is that instance's version, not this computer's app's (ui/design:
// update/instance-and-app). Each frame is the complete window.
import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { AppShell, AppShellFooter } from '@/components/AppShell';
import { VersionBadge } from '@/components/VersionBadge';
import { Workbench } from '@/components/Workbench';
import { DesktopAccountRowView, type DesktopAccountRowViewProps } from '@/desktop/DesktopAccountRowView';
import { InstanceVersionChipView } from '@/desktop/InstanceVersionChip';
import type { DesktopClientProjection } from '@/desktop/bridge';

import { frames, type FrameSpec } from '../../frame';
import { agentsApi } from '../agents/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { homeFactsApi, workbenchFixtures } from '../workbench/fixtures';

type Lang = 'zh' | 'en';
const LOCAL = '小麦的 Mac';
const REMOTE = 'Studio';

const fixturesFor = (lang: Lang) => workbenchFixtures({
  ...agentsApi(),
  ...homeFactsApi({ model: true, push: true, computerUse: true, chatApps: true, paired: true }),
  '/api/session': { remote: false },
  '/api/version': { current: '3.2.7', latest: '3.2.7', has_update: false, error: null },
}, { copy: copyFor(lang === 'en' ? en : zh) });

type Link = 'connected' | 'offline' | 'switching';
const STUDIO = { kind: 'remote', name: REMOTE, instance_id: 'studio', canonical_origin: 'https://studio.avibe.bot' } as const;

const projection = (remote: boolean, update: boolean, link: Link = 'connected', signedIn = true): DesktopClientProjection => ({
  version: 1, revision: 1, generation: 1,
  account: signedIn
    ? { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' } }
    : { status: 'signed_out', identity: null },
  connection: link === 'switching'
    ? { status: 'switching', committed: { kind: 'local', name: LOCAL, instance_id: null }, pending: STUDIO }
    : remote
      ? { status: link === 'offline' ? 'remote_unreachable' : 'remote_connected', committed: STUDIO, pending: null }
      : { status: 'local', committed: { kind: 'local', name: LOCAL, instance_id: null }, pending: null },
  local: { name: LOCAL, remote_access: 'off' },
  desktop: {
    version: '3.2.7',
    update: { phase: update ? 'available' : 'current', latest: update ? '3.2.8' : null, channel: 'stable', skippedVersion: null, interactive: false },
  },
  remote_auxiliary: remote ? 'disabled_incomplete' : 'local',
});

const rowProps = (remote: boolean, update = false, link: Link = 'connected', signedIn = true): Omit<DesktopAccountRowViewProps, 'localUpdater'> => ({
  state: projection(remote, update, link, signedIn), onPresent: () => {},
  // The instance's version, as its own page reads it; gold when it or this computer's app can be updated.
  ...(remote ? { instanceVersion: <InstanceVersionChipView name={REMOTE} version="3.2.7" attention={update} /> } : {}),
});

/** The complete window, with the desktop app's row in place of the Web's when given. */
const shell = (row?: ReactNode) => () => (
  <Routes>
    <Route element={row ? <AppShell footer={({ tools }) => <AppShellFooter tools={tools}>{row}</AppShellFooter>} /> : <AppShell />}>
      <Route path="/" element={<Workbench />} />
    </Route>
  </Routes>
);

const shots: { name: string; title: string; render: () => ReactNode; hover?: string }[] = [
  { name: 'web', title: '侧边栏底部 · Web（运行状态在左，版本号在右）', render: shell() },
  {
    name: 'desktop-local', title: '侧边栏底部 · 桌面应用（本机）',
    render: shell(<DesktopAccountRowView {...rowProps(false)} localUpdater={<VersionBadge openUpward />} />),
  },
  // The trigger's wash shows only under the pointer, with the dot and the name centred in it and
  // the same inset on both sides; no hover tip (Max, 2026-11-20).
  {
    name: 'desktop-local-hover', title: '侧边栏底部 · 桌面应用（本机）· 指针悬停在账户按钮上',
    render: shell(<DesktopAccountRowView {...rowProps(false)} localUpdater={<VersionBadge openUpward />} />),
    hover: '[data-account-trigger]',
  },
  {
    name: 'desktop-signed-out', title: '侧边栏底部 · 桌面应用 · 未登录（登录或注册）',
    render: shell(<DesktopAccountRowView {...rowProps(false, false, 'connected', false)} localUpdater={<VersionBadge openUpward />} />),
  },
  {
    name: 'desktop-signed-out-hover', title: '侧边栏底部 · 桌面应用 · 未登录 · 指针悬停在「登录或注册」上',
    render: shell(<DesktopAccountRowView {...rowProps(false, false, 'connected', false)} localUpdater={<VersionBadge openUpward />} />),
    hover: '[data-account-trigger]',
  },
  {
    name: 'desktop-remote', title: '侧边栏底部 · 桌面应用连接另一台电脑（版本号是那个实例的）',
    render: shell(<DesktopAccountRowView {...rowProps(true)} localUpdater={null} />),
  },
  {
    name: 'desktop-remote-update', title: '侧边栏底部 · 桌面应用连接另一台电脑 · 实例或本机桌面应用有新版本',
    render: shell(<DesktopAccountRowView {...rowProps(true, true)} localUpdater={null} />),
  },
  // The leading glyph changes (dot, offline icon, spinner) in a fixed slot: the name does not move.
  {
    name: 'desktop-remote-offline', title: '侧边栏底部 · 桌面应用连接另一台电脑 · 连接断开',
    render: shell(<DesktopAccountRowView {...rowProps(true, false, 'offline')} localUpdater={null} />),
  },
  {
    name: 'desktop-switching', title: '侧边栏底部 · 桌面应用 · 正在切换到另一台电脑',
    render: shell(<DesktopAccountRowView {...rowProps(false, false, 'switching')} localUpdater={<VersionBadge openUpward />} />),
  },
];

const designFrames = frames(shots.flatMap((shot) => (['zh', 'en'] as const).map((lang): FrameSpec => ({
  name: `${shot.name}-${lang}`,
  title: `${shot.title} · ${lang}`,
  width: 1200,
  height: 800,
  status: 'shipped',
  route: '/',
  fixtures: fixturesFor(lang),
  render: shot.render,
  ...(shot.hover ? { hover: shot.hover } : {}),
}))));

export default designFrames;
