// This computer's update panel while the desktop app shows another computer: a popup
// the shell draws in its own small window over the main one (desktop/src-tauri
// update_popover.rs), the panel at its bottom-left just above the sidebar's bottom
// row. Drawn here as the user sees it: the real window with the shipped panel on top.
import { Route, Routes } from 'react-router-dom';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { AppShell, AppShellFooter } from '@/components/AppShell';
import { Workbench } from '@/components/Workbench';
import { UpdatePanel } from '@/components/update/UpdatePanel';
import { modelFromDesktopState } from '@/components/update/updatePanelModel';
import { DesktopAccountRowView } from '@/desktop/DesktopAccountRowView';
import type { DesktopClientProjection } from '@/desktop/bridge';

import { frames, type FrameSpec } from '../../frame';
import { agentsApi } from '../agents/fixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { homeFactsApi, workbenchFixtures } from '../workbench/fixtures';

const CHECKED_AT = '2026-11-01T07:52:00Z';

const remote: DesktopClientProjection = {
  version: 1, revision: 1, generation: 1,
  account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' } },
  connection: {
    status: 'remote_connected',
    committed: { kind: 'remote', name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio.avibe.bot' },
    pending: null,
  },
  local: { name: '小麦的 Mac', remote_access: 'off' },
  desktop: { version: '3.2.7', update: { phase: 'available', latest: '3.2.8', channel: 'stable', skippedVersion: null, interactive: true } },
  remote_auxiliary: 'disabled_incomplete',
};

/** The popup window: 336 wide, its panel at the bottom-left, 8 px in from the main window's
 *  edge and 40 px above its bottom, as update_popover.rs places it. */
const popup = (
  <div className="absolute bottom-10 left-2 z-50 w-[336px]">
    <div className="ml-2 mb-2 w-80 overflow-hidden rounded-xl border border-edge bg-popover text-popover-foreground shadow-elevation-2">
      <UpdatePanel
        model={modelFromDesktopState(
          { phase: 'available', latest: '3.2.8', channel: 'stable', skippedVersion: null, interactive: true, autoUpdate: false, checkedAt: CHECKED_AT },
          { current: '3.2.7', checkedAt: CHECKED_AT },
        )}
        actions={{ check: { run: () => {} }, update: { run: () => {} }, setAutoUpdate: () => {}, close: () => {} }}
      />
    </div>
  </div>
);

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
          <DesktopAccountRowView state={remote} menuOpen={false} onMenuOpenChange={() => {}} onPresent={() => {}} localUpdater={null} />
        </AppShellFooter>} />}>
          <Route path="/" element={<Workbench />} />
        </Route>
      </Routes>
      {popup}
    </div>
  ),
})));

export default designFrames;
