// Proposed 2026-11-21 (frames only): the Devices page with the states today's backend adds
// (kit/devices-signout.tsx): functions a viewer may or may not change, a Health connection not
// matched to a device, what signing out actually did at avibe.bot, the page a signed-out device sees
// when it comes back too early, and the move to the new address after claiming from another computer.
import type { ReactNode } from 'react';

import { DevicesPageView } from '@/components/devices/DevicesPageView';
import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import type { Device } from '@/components/devices/types';

import { frames, type FrameSpec } from '../../frame';
import {
  FunctionsDialog, OpenNewAddressCard, RecentSignOutsPanel, SignInNotFinishedPage, UnlinkedHealthPanel, type SignOutRow,
} from '../../kit/devices-signout';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';
const copy = { 'settings.sections.remoteAccess': { zh: '设备', en: 'Devices' } };
const fixtures = settingsFixtures({}, { copy });
const t = (zh: string, en: string) => ({ zh, en });
const host = 'max-app.avibe.bot';
const account = 'max@example.test';

const hostDevice: Device = {
  id: 'host', group: 'mine', kind: 'host', name: 'Max 的 MacBook Pro', self: true, online: {},
  client: { kind: 'runtime', version: '3.2.8', platform: 'macOS' },
  capabilities: [{ kind: 'computerUse', on: true }, { kind: 'notifications', on: true }],
};
const phone: Device = {
  id: 'iphone', group: 'mine', kind: 'iphone', name: 'iPhone 15 Pro', online: { path: 'tunnel' },
  client: { kind: 'app', version: '3.2.8', platform: 'iOS 26.1' }, capabilities: [{ kind: 'notifications', on: true }],
};

const page = (extra: { afterDevices?: ReactNode; overlay?: ReactNode } = {}) => settingsPage('remote-access', (
  <>
    <DevicesPageView
      reach={{ state: 'on', host, account, tunnel: 'good', ms: 120 }}
      ownerEmail={account}
      devices={[hostDevice, phone]}
      afterDevices={extra.afterDevices}
    />
    {extra.overlay}
  </>
));

const signOuts: SignOutRow[] = [
  { device: 'iPhone 14', at: t('今天 10:20', 'Today 10:20'), account: 'accepted', health: 'cleanup_pending' },
  { device: 'Chrome · Windows', at: t('今天 10:18', 'Today 10:18'), account: 'pending' },
  { device: 'Safari · iPad', at: t('今天 09:50', 'Today 09:50'), account: 'not_applicable' },
  { device: 'Firefox · Linux', at: t('昨天 22:05', 'Yesterday 22:05'), account: 'unconfirmed' },
  { device: 'Edge · Windows', at: t('昨天 21:40', 'Yesterday 21:40'), account: 'refused' },
];

const WIDE = { width: 1440, height: 1100 };
const PHONE = { width: 375, height: 1500 };
const f = (name: string, title: string, size: { width: number; height: number }, render: FrameSpec['render'], extra: Partial<FrameSpec> = {}): FrameSpec => ({
  name, title, ...size, status: 'proposed', themes: ['light'], route, fixtures, render, ...extra,
});

const designFrames = frames([
  f('can-change', '功能 · 能切换的画开关；健康只在已连接时可关（关时先确认，见 settings/devices-rename 之外的健康确认帧）', WIDE, page({
    overlay: <FunctionsDialog name="iPhone 15 Pro" app="Avibe App 3.2.8 · iOS 26.1" functions={[
      { kind: 'notifications', on: true, canChange: true },
      { kind: 'health', on: true, canChange: true, detail: t('步数、睡眠 · 今天 08:12 同步', 'Steps and sleep · synced today at 08:12') },
      { kind: 'schedule', on: false, canChange: true },
    ]} />,
  }), { themes: ['light', 'dark'] }),
  f('read-only', '功能 · 这个查看者不能切换的：只读文字，不出现点了才被拒的开关', WIDE, page({
    overlay: <FunctionsDialog name="iPhone 15 Pro" app="sara@example.test · Avibe App 3.2.8" functions={[
      { kind: 'notifications', on: true, canChange: false },
      { kind: 'health', on: false, canChange: false, detail: t('没有连接。只能在 iPhone 上连接。', 'Not connected. It can be connected only on the iPhone.') },
      { kind: 'schedule', on: true, canChange: false },
    ]} note={t('这是别人的设备：它的功能由它的主人在那台设备上决定。', 'This is someone else’s device: its owner decides its functions on that device.')} />,
  })),
  f('computer-use', '电脑操作的四种状态都只读：已就绪 / 需要授权 / 未开启 / 暂时不可用', WIDE, page({
    overlay: <FunctionsDialog name="Max 的 MacBook Pro" app="Avibe 3.2.8 · macOS" functions={[
      { kind: 'computerUse', status: 'needs_permission' },
      { kind: 'notifications', on: true, canChange: true },
    ]} />,
  })),
  f('unlinked-health', '未对应到设备的健康连接：至多一项，可以单独断开', WIDE, page({
    afterDevices: <UnlinkedHealthPanel device="Max 的 iPhone" synced={t('11 月 18 日', 'Nov 18')} />,
  })),
  f('sign-out-results', '退出的五种结果，各一句；坐在电脑前退出时健康清理写「尚未确认」', { width: 1440, height: 1300 }, page({
    afterDevices: <RecentSignOutsPanel rows={signOuts} />,
  })),
  f('sign-out-health-skipped', '没能确认是所有者在电脑前：健康连接没有断开，指向单独断开的那一行', { width: 1440, height: 1200 }, page({
    afterDevices: (
      <>
        <UnlinkedHealthPanel device="iPhone 14" synced={t('今天 07:40', 'Today 07:40')} />
        <RecentSignOutsPanel rows={[{ device: 'iPhone 14', at: t('今天 10:20', 'Today 10:20'), account: 'not_applicable', health: 'skipped_unverified' }]} />
      </>
    ),
  })),
  f('sign-out-results-phone', '退出的结果 · 375 宽', PHONE, page({ afterDevices: <RecentSignOutsPanel rows={signOuts} /> })),
  f('sign-in-not-finished', '被请出去的设备在撤销完成前回来：「登录未完成」', { width: 1440, height: 800 }, () => <SignInNotFinishedPage />),
  f('sign-in-not-finished-phone', '「登录未完成」 · 375 宽', { width: 375, height: 760 }, () => <SignInNotFinishedPage />),
  f('open-new-address', '从局域网里另一台电脑领取成功：请打开新地址（带链接）', { width: 1440, height: 700 }, settingsPage('remote-access', (
    <SettingsPageShell title="设备" subtitle="" activeTab="remote-access"><OpenNewAddressCard host={host} /></SettingsPageShell>
  ))),
  f('open-new-address-phone', '请打开新地址 · 375 宽', { width: 375, height: 760 }, settingsPage('remote-access', (
    <SettingsPageShell title="设备" subtitle="" activeTab="remote-access"><OpenNewAddressCard host={host} /></SettingsPageShell>
  ))),
]);

export default designFrames;
