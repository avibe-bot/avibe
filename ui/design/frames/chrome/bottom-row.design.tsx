// The sidebar's bottom row on every surface, as shipped: the Web, the desktop app
// on this computer, and the desktop app showing another computer. One component
// (`SidebarStatusRow`): the status on the left, the version chip on the right.
// Each frame is the complete window.
import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { AppShell, AppShellFooter } from '@/components/AppShell';
import { VersionBadge } from '@/components/VersionBadge';
import { Workbench } from '@/components/Workbench';
import { DesktopAccountRowView, type DesktopAccountRowViewProps } from '@/desktop/DesktopAccountRowView';
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

const projection = (remote: boolean, update: boolean): DesktopClientProjection => ({
  version: 1, revision: 1, generation: 1,
  account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' } },
  connection: {
    status: remote ? 'remote' : 'local',
    committed: remote ? { kind: 'remote', name: REMOTE, instance_id: 'studio' } : { kind: 'local', name: LOCAL, instance_id: null },
    pending: null,
  },
  local: { name: LOCAL, remote_access: 'off' },
  desktop: {
    version: '3.2.7',
    update: { phase: update ? 'available' : 'current', latest: update ? '3.2.8' : null, channel: 'stable', skippedVersion: null, interactive: false },
  },
  remote_auxiliary: remote ? 'disabled_incomplete' : 'complete',
} as DesktopClientProjection);

const rowProps = (remote: boolean, update = false): Omit<DesktopAccountRowViewProps, 'localUpdater'> => ({
  state: projection(remote, update), menuOpen: false, onMenuOpenChange: () => {}, onPresent: () => {},
});

/** The complete window, with the desktop app's row in place of the Web's when given. */
const shell = (row?: ReactNode) => () => (
  <Routes>
    <Route element={row ? <AppShell footer={({ tools }) => <AppShellFooter tools={tools}>{row}</AppShellFooter>} /> : <AppShell />}>
      <Route path="/" element={<Workbench />} />
    </Route>
  </Routes>
);

const shots: { name: string; title: string; render: () => ReactNode }[] = [
  { name: 'web', title: '侧边栏底部 · Web（运行状态在左，版本号在右）', render: shell() },
  {
    name: 'desktop-local', title: '侧边栏底部 · 桌面应用（本机）',
    render: shell(<DesktopAccountRowView {...rowProps(false)} localUpdater={<VersionBadge openUpward />} />),
  },
  {
    name: 'desktop-remote', title: '侧边栏底部 · 桌面应用连接另一台电脑（版本号同样是可点的版本号）',
    render: shell(<DesktopAccountRowView {...rowProps(true)} localUpdater={null} />),
  },
  {
    name: 'desktop-remote-update', title: '侧边栏底部 · 桌面应用连接另一台电脑 · 本机桌面应用有新版本',
    render: shell(<DesktopAccountRowView {...rowProps(true, true)} localUpdater={null} />),
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
}))));

export default designFrames;
