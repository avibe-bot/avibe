// This computer's update panel while the desktop app shows another computer: a popup
// the shell draws in its own small window over the main one (desktop/src-tauri
// update_popover.rs), the panel at its bottom-left just above the sidebar's bottom
// row. Drawn as the user sees it: the real window, and the shipped popup page
// (UpdatePopoverView with DesktopUpdatePanel) in a box placed where the shell puts
// its 336 × 520 window, 8 px in from the left and 40 px above the bottom.
import { Route, Routes } from 'react-router-dom';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { AppShell, AppShellFooter } from '@/components/AppShell';
import { Workbench } from '@/components/Workbench';
import { DesktopAccountRowView } from '@/desktop/DesktopAccountRowView';
import { DesktopUpdatePanel, UpdatePopoverView } from '@/desktop/AccountApp';
import type { DesktopClientProjection, DesktopClientState } from '@/desktop/bridge';

import { frames, type FrameSpec } from '../../frame';
import { agentsApi } from '../agents/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { homeFactsApi, workbenchFixtures } from '../workbench/fixtures';

const projection: DesktopClientProjection = {
  version: 1, revision: 1, generation: 1,
  account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' } },
  connection: {
    status: 'remote_connected',
    committed: { kind: 'remote', name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio.avibe.bot' },
    pending: null,
  },
  local: { name: '小麦的 Mac', remote_access: 'off' },
  desktop: {
    version: '3.2.7',
    update: { phase: 'available', latest: '3.2.8', channel: 'stable', skippedVersion: null, interactive: true, autoUpdate: false, checkedAt: '2026-11-01T07:52:00Z', hostActions: ['check', 'prereleases', 'auto_update', 'install'] },
  },
  remote_auxiliary: 'disabled_incomplete',
};

// The popup page reads the full client state; only its update fields matter here.
const state = { ...projection, account: { ...projection.account, email: null, gate: null, methods: [] } } as unknown as DesktopClientState;
const install = { failed: false, accept: () => {}, reset: () => {} } as Parameters<typeof DesktopUpdatePanel>[0]['install'];

const designFrames = frames((['zh', 'en'] as const).map((lang): FrameSpec => ({
  name: `open-${lang}`,
  title: `版本面板 · 连接另一台电脑时也是弹出层（本机桌面应用绘制）· ${lang}`,
  width: 1200,
  height: 800,
  status: 'shipped',
  route: '/',
  fixtures: workbenchFixtures({
    ...agentsApi(),
    ...homeFactsApi({ model: true, push: true, computerUse: true, chatApps: true, paired: true }),
    '/api/session': { remote: false },
    '/api/version': { current: '3.2.7', latest: '3.2.7', has_update: false, error: null },
  }, { copy: copyFor(lang === 'en' ? en : zh) }),
  render: () => (
    <div className="relative h-full">
      <Routes>
        <Route element={<AppShell footer={({ tools }) => <AppShellFooter tools={tools}>
          <DesktopAccountRowView state={projection} menuOpen={false} onMenuOpenChange={() => {}} onPresent={() => {}} localUpdater={null} />
        </AppShellFooter>} />}>
          <Route path="/" element={<Workbench />} />
        </Route>
      </Routes>
      {/* The popup window: `fixed` inside UpdatePopoverView resolves to this transformed box. */}
      {/* Spacing-scale sizes: w-84 is 336 px and h-130 is 520 px, the shell's window. */}
      <div className="absolute bottom-10 left-2 z-50 h-130 w-84 transform-gpu">
        <UpdatePopoverView label={lang === 'en' ? en.dashboard.versionAndUpdate : zh.dashboard.versionAndUpdate} onClose={() => {}}>
          <DesktopUpdatePanel state={state} act={async () => true} busy={false} install={install} close={() => {}} />
        </UpdatePopoverView>
      </div>
    </div>
  ),
})));

export default designFrames;
