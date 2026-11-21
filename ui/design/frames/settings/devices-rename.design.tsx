// Proposed 2026-11-21 (frames only): changing the domain under the owner's 11:05 rules, and binding
// remote access again (kit/devices-rename.tsx). The old name is held for a waiting period (one
// number; 30 days now) in which only this account can take it back; renaming clears what it can by
// itself and says plainly what it cannot. The dialogs open over the shipped 「连接方式」 page at the
// computer itself; 「重新绑定」 sits on the Devices page under how this Avibe is reached.
import { frames, type FrameSpec } from '../../frame';
import { ConnectionPageView, type ConnectionPageViewProps } from '@/components/devices/ConnectionPageView';
import { DevicesPageView } from '@/components/devices/DevicesPageView';
import type { Device } from '@/components/devices/types';
import { ClaimLimitCard, MovedPage, RebindDialog, RebindRow, RenameDialog, UnrecognizedChangeBanner, type RenameDevice, type RenameDialogProps } from '../../kit/devices-rename';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';
const copy = { 'settings.sections.remoteAccess': { zh: '设备', en: 'Devices' } };
const fixtures = settingsFixtures({}, { copy });

/** The real day the old name is released: 30 days (plus a margin) on, or the day the last direct
 *  certificate signed for it runs out, whichever is later (Product, 11:26). Days are always real. */
const t = (zh: string, en: string) => ({ zh, en });
const RELEASE_ON = t('12 月 23 日', 'December 23');
/** The most names one account may hold, counting names still waiting (owner, 11:20; one number, per plan later). */
const NAME_LIMIT = 5;
const host = 'max-app.avibe.bot';
const account = 'max@example.test';

const page: ConnectionPageViewProps = {
  viewer: 'local',
  host,
  direct: 'ready',
  relay: false,
  tunnel: {
    ms: 120,
    active: 'quic',
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: '4', edge_bind_address: '' },
    interfaces: [],
  },
};

const devices: RenameDevice[] = [
  { name: 'Max 的 MacBook Pro', kind: 'host', self: true },
  { name: 'iPhone 15 Pro', kind: 'iphone' },
  { name: 'Mac mini（办公室）', kind: 'desktop' },
  { name: 'Safari · iPad', kind: 'homeScreen' },
  { name: 'Chrome · Windows', kind: 'browser' },
];
const mixed: RenameDevice[] = devices.map((each) => ({
  ...each,
  ...(each.kind === 'homeScreen'
    ? { cleanup: 'unconfirmed' as const, lastActive: t('昨天', 'yesterday') }
    : each.kind === 'browser'
      ? { cleanup: 'unconfirmed' as const, lastActive: t('11月18日', 'Nov 18') }
      : { cleanup: 'cleared' as const }),
}));
const cleared: RenameDevice[] = devices.map((each) => ({ ...each, cleanup: 'cleared' as const }));

const base: RenameDialogProps = { step: 'form', host, name: 'maxwell', releaseOn: RELEASE_ON, devices, health: true };

const WIDE = { width: 1440, height: 1380 };
const PHONE = { width: 375, height: 900 };

function frame(name: string, title: string, size: { width: number; height: number }, props: RenameDialogProps, extra: Partial<FrameSpec> = {}): FrameSpec {
  return {
    name, title, ...size, status: 'proposed', themes: ['light'], route, fixtures,
    render: settingsPage('remote-access', (
      <>
        <ConnectionPageView {...page} host={props.step === 'done' ? `${props.name}-app.avibe.bot` : props.host} />
        <RenameDialog {...props} />
      </>
    )),
    ...extra,
  };
}

const hostDevice: Device = {
  id: 'host', group: 'mine', kind: 'host', name: 'Max 的 MacBook Pro', self: true, online: {},
  client: { kind: 'runtime', version: '3.2.8', platform: 'macOS' },
  capabilities: [{ kind: 'computerUse', on: true }, { kind: 'notifications', on: true }],
};
const phone: Device = {
  id: 'iphone', group: 'mine', kind: 'iphone', name: 'iPhone 15 Pro', online: { path: 'tunnel' },
  client: { kind: 'app', version: '3.2.8', platform: 'iOS 26.1' }, capabilities: [{ kind: 'notifications', on: true }],
};
const devicesPage = (dialog: boolean, banner = false) => settingsPage('remote-access', (
  <>
    <DevicesPageView
      reach={{ state: 'on', host, account, tunnel: 'good', ms: 120, direct: true }}
      ownerEmail={account}
      devices={[hostDevice, phone]}
      afterReach={banner ? <UnrecognizedChangeBanner host="sam-app.avibe.bot" /> : <RebindRow />}
    />
    {dialog && <RebindDialog host={host} account={account} />}
  </>
));

const designFrames = frames([
  // Before: the new name, what happens, the one thing that cannot be cleared, and the devices.
  frame('form', '更改域名 · 第一步：新名称、改名后会怎样、清不到的那一处（写法 A）、设备和怎么在上面退出', WIDE, base, { themes: ['light', 'dark'] }),
  frame('form-residue-b', '更改域名 · 清不到的那一处：写法 B', WIDE, { ...base, residue: 'b' }),
  frame('form-residue-c', '更改域名 · 清不到的那一处：写法 C', WIDE, { ...base, residue: 'c' }),
  frame('form-phone', '更改域名 · 第一步 · 375 宽', PHONE, base),
  frame('reclaim', '更改域名 · 拿回自己等待期内的旧名字：点一下就选回去，不占额度', WIDE,
    { ...base, host: 'maxwell-app.avibe.bot', name: 'max', held: [{ name: 'max', until: t('11 月 24 日', 'November 24') }] }),
  frame('reject-taken', '更改域名 · 名称已被占用', WIDE, { ...base, name: 'alex', reject: { kind: 'taken' } }),
  frame('reject-held', '更改域名 · 这个名字在别人的等待期里', WIDE, { ...base, name: 'sam', reject: { kind: 'held' } }),
  // Renaming may end at the limit plus the name just left, so it is refused at 6 (the API's real count).
  frame('reject-limit', '更改域名 · 账号已持有 6 个名字（含冷却中的），写明最早哪天释放；下半部分照常显示', WIDE,
    { ...base, name: 'maxim', reject: { kind: 'renameLimit', count: NAME_LIMIT + 1, released: t('12 月 21 日', 'December 21') }, held: [{ name: 'studio', until: t('12 月 21 日', 'December 21') }] }),
  frame('reject-limit-collapsed', '更改域名 · 同一拒绝，收起后果、提醒和设备清单（供比较）', { width: 1440, height: 800 },
    { ...base, name: 'maxim', reject: { kind: 'renameLimit', count: NAME_LIMIT + 1, released: t('12 月 21 日', 'December 21') }, held: [{ name: 'studio', until: t('12 月 21 日', 'December 21') }], collapseOnReject: true }),
  frame('reject-limit-collapsed-phone', '更改域名 · 收起的拒绝 · 375 宽', PHONE,
    { ...base, name: 'maxim', reject: { kind: 'renameLimit', count: NAME_LIMIT + 1, released: t('12 月 21 日', 'December 21') }, held: [{ name: 'studio', until: t('12 月 21 日', 'December 21') }], collapseOnReject: true }),
  frame('confirm', '更改域名 · 第二次确认：按钮先停 3 秒', { width: 1440, height: 900 }, { ...base, step: 'confirm', countdown: 3 }),
  // After: the same list, each device cleared or not confirmed yet; the reminder stays.
  frame('done-mixed', '改名之后 · 同一张清单：已清理 / 未确认 · 上次活跃，仍带清不到的那句', { width: 1440, height: 1100 },
    { ...base, step: 'done', host, name: 'maxwell', devices: mixed }),
  frame('done-mixed-phone', '改名之后 · 混合状态 · 375 宽', PHONE, { ...base, step: 'done', host, name: 'maxwell', devices: mixed }),
  frame('done-cleared', '改名之后 · 全部已清理：仍然说清单外的浏览器清不到', { width: 1440, height: 1100 },
    { ...base, step: 'done', host, name: 'maxwell', devices: cleared }),
  // Any browser opening the old address during the waiting period: cleared, then one sentence and a link.
  { name: 'moved', title: '旧地址 · 等待期内打开：清掉本机数据后，一句话加 avibe.bot 的链接', width: 1440, height: 800, status: 'proposed', themes: ['light'],
    fixtures, render: () => <MovedPage /> },
  { name: 'moved-phone', title: '旧地址 · 已迁移 · 375 宽', width: 375, height: 700, status: 'proposed', themes: ['light'],
    fixtures, render: () => <MovedPage /> },
  // The claim refused for the same reason, on the Devices page while remote access is off.
  { name: 'claim-limit', title: '领取 · 账号已持有 5 个名字：拒绝，写明最早释放日', width: 1440, height: 640, status: 'proposed', themes: ['light'],
    route, fixtures, render: settingsPage('remote-access', <ClaimLimitCard account={account} name="max-macmini" reject={{ kind: 'limit', limit: NAME_LIMIT, released: t('12 月 21 日', 'December 21') }} />) },
  { name: 'claim-limit-in-use', title: '领取 · 5 个名字都在用（没有等待中的）：先删除一个实例', width: 1440, height: 640, status: 'proposed', themes: ['light'],
    route, fixtures, render: settingsPage('remote-access', <ClaimLimitCard account={account} name="max-macmini" reject={{ kind: 'limitInUse', limit: NAME_LIMIT }} />) },
  // Binding remote access again, at the computer itself: the old device key stops working.
  { name: 'rebind', title: '设备页 · 「重新绑定远程访问」入口（在运行 Avibe 的电脑上）', width: 1440, height: 900, status: 'proposed', themes: ['light'],
    route, fixtures, render: devicesPage(false) },
  { name: 'rebind-confirm', title: '重新绑定 · 确认：旧的设备密钥立即作废；之后可拿回等待期内的旧名字', width: 1440, height: 900, status: 'proposed', themes: ['light'],
    route, fixtures, render: devicesPage(true) },
  { name: 'unrecognized-change', title: '设备页 · 地址在这台 Avibe 之外被改了：醒目提示，先重新绑定再拿回名字', width: 1440, height: 900, status: 'proposed', themes: ['light'],
    route, fixtures, render: devicesPage(false, true) },
]);

export default designFrames;
