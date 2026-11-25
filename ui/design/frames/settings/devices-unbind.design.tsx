// 「解除绑定」 on 「连接方式」 (src/components/devices/UnbindViews.tsx), the owner's, beside
// 「更改域名」. One confirmation that says what is lost and that the way back is 「开启远程访问」;
// then the instance does it by itself and continues on its own. Pressed from a device away from
// the computer, that device loses the page at once and the confirmation says so. Every frame draws
// the shipped views with the data the pages would feed them.
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { frames, type FrameSpec } from '../../frame';
import { ConnectionPageView, type ConnectionPageViewProps } from '@/components/devices/ConnectionPageView';
import { DevicesPageView, type DevicesPageViewProps } from '@/components/devices/DevicesPageView';
import type { AddressState, ConnectionState, Device } from '@/components/devices/types';
import { settingsFixtures, settingsPage } from './fixtures';

const host = 'max-app.avibe.bot';
const RELEASE = '2026-12-25T00:00:00.000Z';
const CONNECTION = '/settings/remote-access/connection';
const DEVICES = '/settings/remote-access';

const connection: ConnectionState = {
  settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' },
  interfaces: [], direct: 'ready',
};
const address: AddressState = { hostname: host, eligible: true, holdDays: 30, releaseAtIfChangedNow: RELEASE, retired: [], healthConnected: true };
const base: ConnectionPageViewProps = {
  load: 'ready', connection, tunnel: { state: 'up', ms: 120, active: 'quic' }, remote: false,
  host, address, rebind: { state: 'ready' }, unbind: { state: 'ready' },
};
const away: Partial<ConnectionPageViewProps> = { remote: true, via: 'tunnel', rebind: { state: 'remote' } };
/** The computer by the name the Devices page lists it under. */
const COMPUTER = 'Max 的 MacBook Pro';
const confirm = (remote: boolean, extra: Partial<NonNullable<ConnectionPageViewProps['unbindConfirm']>> = {}) => ({
  remote, computer: COMPUTER, healthConnected: true, releaseAt: RELEASE, holdDays: 30, ...extra,
});

const WIDE = { width: 1440, height: 1000 };
/** The whole 连接方式 page, down to the domain's row. */
const PAGE = { width: 1440, height: 1300 };
const PAGE_PHONE = { ...IPHONE_17_PRO, width: 390, height: 2600 };
const PHONE = { ...IPHONE_17_PRO, width: 390, height: 1100 };

function frame(name: string, title: string, size: { width: number; height: number; safeArea?: { top: number; bottom: number } }, props: Partial<ConnectionPageViewProps>, extra: Partial<FrameSpec> & { app?: boolean } = {}): FrameSpec {
  const { app, ...spec } = extra;
  return {
    name, title, ...size, status: 'proposed', themes: ['light'], route: CONNECTION,
    fixtures: settingsFixtures({}, app ? { nativeShell: IOS_APP_CAPABILITIES } : {}),
    render: settingsPage('remote-access/connection', <ConnectionPageView {...base} {...props} />),
    ...spec,
  };
}

const macbook: Device = {
  id: 'host', group: 'mine', kind: 'host', name: 'Max 的 MacBook Pro', self: true, client: { kind: 'runtime', version: '3.2.8', platform: 'macOS' },
  functions: [{ kind: 'computerUse', status: 'ready' }, { kind: 'notifications', on: true, canChange: true }],
};
const devicesBase: DevicesPageViewProps = { reach: { state: 'off', surface: 'browser' }, devices: [macbook] };
function devices(name: string, title: string, size: { width: number; height: number; safeArea?: { top: number; bottom: number } }, props: Partial<DevicesPageViewProps>): FrameSpec {
  return {
    name, title, ...size, status: 'proposed', themes: ['light'], route: DEVICES,
    fixtures: settingsFixtures({}),
    render: settingsPage('remote-access', <DevicesPageView {...devicesBase} {...props} />),
  };
}

const designFrames = frames([
  // 1. The entry: beside 「更改域名」, the same weight, the owner's.
  frame('entry', '连接方式 · 「解除绑定」和「更改域名」在同一行、同样的分量，只给所有者', PAGE, {}),
  frame('entry-phone', '入口 · 390 宽', PAGE_PHONE, {}),

  // 2. The confirmation: what ends, what is removed for good, what stays, and the way back.
  frame('confirm', '确认框 · 在这台电脑上按：丢掉什么、名字保留到哪天、工作区留着、回来的路是「开启远程访问」', WIDE, { unbindConfirm: confirm(false) }),
  frame('confirm-phone', '确认框 · 在这台电脑上按 · 390 宽', PHONE, { unbindConfirm: confirm(false) }),
  frame('confirm-remote', '确认框 · 从远程设备上按：电脑一律叫它的名字；多一句，这台设备会马上断开，要回到那台电脑上开启', WIDE, { ...away, unbindConfirm: confirm(true) }),
  frame('confirm-remote-phone', '确认框 · 从远程设备（iPhone App）上按 · 390 宽', PHONE, { ...away, unbindConfirm: confirm(true) }, { app: true }),
  frame('confirm-no-health', '确认框 · 没有同步过健康数据：少一条', WIDE, { unbindConfirm: confirm(false, { healthConnected: false }) }),
  frame('confirm-no-date', '确认框 · 实例没给日子：说保留天数，不编日期', WIDE, { unbindConfirm: confirm(false, { releaseAt: undefined }) }),
  frame('confirm-busy', '确认框 · 按了「解除绑定」，正在交给实例', WIDE, { unbindConfirm: confirm(false, { busy: true }) }),

  // 3. Running: one sentence, no cancel.
  frame('unbinding', '进行中 · 连接方式：一句「正在解除绑定，会自动继续。」，没有取消，没有别的按钮', PAGE, { unbind: { state: 'unbinding' } }),
  devices('unbinding-devices', '进行中 · 设备页：同一句', WIDE, { reach: { state: 'unknown' }, unbind: { state: 'unbinding' } }),

  // 4. Done: the Devices page as on a new install, with what happened and how long the name stays.
  devices('done', '完成 · 设备页回到没绑定的样子，带「开启远程访问」；说名字保留到哪天', WIDE, { unbind: { state: 'done', host, releaseAt: RELEASE } }),
  devices('done-phone', '完成 · 390 宽', PHONE, { unbind: { state: 'done', host, releaseAt: RELEASE } }),

  // 5. Nothing changed: the reason, and remote access is as it was before.
  frame('not-changed', '什么都没变 · avibe.bot 没有同意：远程访问恢复到按下之前，可以再按', PAGE, { unbind: { state: 'notChanged', reason: 'refused' } }),
  frame('not-changed-ineligible', '什么都没变 · 这个 Avibe 不能在这里解除绑定', PAGE, { unbind: { state: 'notChanged', reason: 'ineligible' } }),

  // 6. Only this computer can sort it out: the page's existing sentence, no new one.
  frame('needs-repair', '需要在这台电脑上处理 · 复用已有的那句，不加新句子；按钮都收起', PAGE, { unbind: { state: 'needsRepair' } }),

  // 7. 「作为一台新电脑重新绑定」 now deletes the old binding at avibe.bot first; where avibe.bot does
  //    not accept that (its key no longer works), a separate confirmation clears this computer only.
  frame('as-new-cloud', '作为一台新电脑重新绑定 · 先在 avibe.bot 上删除原来的 Avibe：名字为原来的账号保留到哪天', { width: 1440, height: 1200 },
    { rebind: { state: 'asNewOffered', reason: 'predecessor_unrecoverable' }, canReset: true, rebindDialog: { kind: 'asNewCloud', healthConnected: true, releaseAt: RELEASE, holdDays: 30 } }),
  frame('as-new-cloud-other-account', '作为一台新电脑重新绑定 · 属于另一个账号：先说是谁的，名字为那个账号保留', { width: 1440, height: 1200 },
    { rebind: { state: 'asNewOffered', reason: 'other_account', hint: 'm***@gmail.com' }, canReset: true, rebindDialog: { kind: 'asNewCloud', healthConnected: false, releaseAt: RELEASE, holdDays: 30 } }),
  frame('as-new-cloud-phone', '作为一台新电脑重新绑定（删除云端）· 390 宽', { ...IPHONE_17_PRO, width: 390, height: 1500 },
    { rebind: { state: 'asNewOffered', reason: 'predecessor_unrecoverable' }, canReset: true, rebindDialog: { kind: 'asNewCloud', healthConnected: true, releaseAt: RELEASE, holdDays: 30 } }),
  frame('as-new-local-after-401', 'avibe.bot 没有接受删除（钥匙已经不能用）：单独确认，只清这台电脑', { width: 1440, height: 1250 },
    { rebind: { state: 'asNewOffered', reason: 'predecessor_unrecoverable' }, canReset: true, rebindDialog: { kind: 'asNew', healthConnected: true, afterRefusal: true } }),
  frame('as-new-local-after-401-phone', '只清这台电脑 · 390 宽', { ...IPHONE_17_PRO, width: 390, height: 1700 },
    { rebind: { state: 'asNewOffered', reason: 'predecessor_unrecoverable' }, canReset: true, rebindDialog: { kind: 'asNew', healthConnected: true, afterRefusal: true } }),
]);

export default designFrames;
