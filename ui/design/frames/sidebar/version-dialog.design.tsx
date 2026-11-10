// The version dialog (click the version badge) with the pre-release switch: off offers stable
// releases only; on offers the newest of stable and pre-release through the same Install button.
import { Route, Routes } from 'react-router-dom';

import { AppShell } from '@/components/AppShell';
import { MobileDockDrawer } from '@/components/apps/MobileDockDrawer';
import { InboxPage } from '@/components/workbench/InboxPage';

import { frames } from '../../frame';
import { IPHONE_17_PRO } from '../../shell/fixtures';
import { settingsConfig } from '../settings/fixtures';
import { showPagesApi } from '../show-pages/fixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

type Version = { current: string; latest: string; has_update: boolean };

const fixtures = (prereleases: boolean, version: Version) => workbenchFixtures({
  ...showPagesApi(),
  '/api/config': { ...settingsConfig, update: { ...settingsConfig.update, auto_update: true, prereleases } },
  '/api/version': { ...version, error: null, build: { kind: 'package' } },
});

// Off: 3.2.5 is the newest stable release, so nothing is offered.
const off = fixtures(false, { current: '3.2.5', latest: '3.2.5', has_update: false });
// On: the newest pre-release is newer than every stable release, so it is offered.
const on = fixtures(true, { current: '3.2.5', latest: '3.2.6rc2', has_update: true });
// Turned off again while running a pre-release: the older stable is never offered as a downgrade.
const offOnRc = fixtures(false, { current: '3.2.6rc2', latest: '3.2.5', has_update: false });

const open = (current: string) => [`role=button[name="v${current}"]`];
// On a phone the badge sits in the Apps drawer's More sheet.
const openOnPhone = (current: string) => ['role=button[name="{t:nav.more}"]', ...open(current)];

const desktop = () => (
  <Routes>
    <Route element={<AppShell />}>
      <Route path="/inbox" element={<div />} />
    </Route>
  </Routes>
);

const phone = phoneShell(
  '/inbox',
  <>
    <InboxPage onOpenSearch={() => {}} />
    <MobileDockDrawer open onClose={() => {}} />
  </>,
);

const designFrames = frames([
  {
    name: 'desktop-off',
    title: '版本弹窗 · 预览版关闭（只推正式版）',
    width: 1000,
    height: 640,
    status: 'proposed',
    route: '/inbox',
    fixtures: off,
    click: open('3.2.5'),
    render: desktop,
  },
  {
    name: 'desktop-on',
    title: '版本弹窗 · 预览版开启（推送 RC）',
    width: 1000,
    height: 640,
    status: 'proposed',
    route: '/inbox',
    fixtures: on,
    click: open('3.2.5'),
    render: desktop,
  },
  {
    name: 'desktop-off-on-rc',
    title: '版本弹窗 · 在 RC 上关闭（不降级）',
    width: 1000,
    height: 640,
    status: 'proposed',
    route: '/inbox',
    fixtures: offOnRc,
    click: open('3.2.6rc2'),
    render: desktop,
  },
  {
    name: 'phone-off',
    title: '版本弹窗 · 手机 · 预览版关闭',
    width: IPHONE_17_PRO.width,
    height: IPHONE_17_PRO.height,
    safeArea: IPHONE_17_PRO.safeArea,
    status: 'proposed',
    route: '/inbox',
    fixtures: off,
    click: openOnPhone('3.2.5'),
    render: phone,
  },
  {
    name: 'phone-on',
    title: '版本弹窗 · 手机 · 预览版开启',
    width: IPHONE_17_PRO.width,
    height: IPHONE_17_PRO.height,
    safeArea: IPHONE_17_PRO.safeArea,
    status: 'proposed',
    route: '/inbox',
    fixtures: on,
    click: openOnPhone('3.2.5'),
    render: phone,
  },
]);

export default designFrames;
