// Changing this Avibe's domain, and binding remote access again
// (src/components/devices/RenameDialogs.tsx, RebindViews.tsx), over the shipped 「连接方式」 page.
// The old name stops working at once and is kept for this account until the day avibe.bot gives
// (never a fixed number of days where a day is known); renaming clears what it can by itself and
// says plainly what it cannot. 「重新绑定」 is a quiet row beside 「更改域名」, at the computer that
// runs Avibe only; binding as a new computer is offered only after avibe.bot refused in one of the
// two ways that allow it. Contracts: docs/plans/instance-address-change.md,
// local-remote-access-rebind.md.
import { DESIGN_NOW } from '../../shell/clock';
import { frames, type FrameSpec } from '../../frame';
import { ConnectionPageView, type ConnectionPageViewProps } from '@/components/devices/ConnectionPageView';
import type { AddressState, ConnectionState, RebindState, RenameDevice, RetiredName } from '@/components/devices/types';
import { settingsFixtures, settingsPage } from './fixtures';

const host = 'max-app.avibe.bot';
const newHost = 'maxwell-app.avibe.bot';
const RELEASE = '2026-12-23T00:00:00.000Z';
const ago = (minutes: number) => new Date(DESIGN_NOW - minutes * 60_000).toISOString();

const connection: ConnectionState = {
  settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' },
  interfaces: [], direct: 'ready',
};
const address = (extra: Partial<AddressState> = {}): AddressState => ({
  hostname: host, eligible: true, holdDays: 30, releaseAtIfChangedNow: RELEASE, retired: [], healthConnected: true, ...extra,
});

const devices: RenameDevice[] = [
  { id: 'host', name: 'Max 的 MacBook Pro', kind: 'host', self: true },
  { id: 'iphone', name: 'iPhone 15 Pro', kind: 'iphone' },
  { id: 'mini', name: 'Mac mini（办公室）', kind: 'desktop' },
  { id: 'safari', name: 'Safari · iPad', kind: 'homeScreen' },
  { id: 'chrome', name: null, kind: 'browser' },
];
const mixed: RenameDevice[] = devices.filter((each) => each.kind !== 'host').map((each) => ({
  ...each,
  self: undefined,
  ...(each.kind === 'homeScreen'
    ? { cleanup: 'unconfirmed' as const, lastActiveAt: ago(1125) }
    : each.kind === 'browser' ? { cleanup: 'unconfirmed' as const, lastActiveAt: ago(3 * 24 * 60) } : { cleanup: 'cleared' as const }),
}));
const withGuest: RenameDevice[] = [...mixed, { id: 'sara', name: 'Chrome · macOS', kind: 'browser', account: 'sara@example.test', person: 'sara@example.test', cleanup: 'cleared' }];
const retiredMax: RetiredName = { hostname: host, releaseAt: RELEASE, devices: mixed };
const studio: RetiredName = { hostname: 'studio-app.avibe.bot', releaseAt: '2026-12-21T00:00:00.000Z', devices: [] };

const base: ConnectionPageViewProps = {
  load: 'ready', connection, tunnel: { state: 'up', ms: 120, active: 'quic' }, remote: false,
  host, address: address(), renameDevices: devices, rebind: { state: 'ready' },
};

const WIDE = { width: 1440, height: 1380 };
const MID = { width: 1440, height: 1000 };
const PHONE = { width: 375, height: 900 };

const CONNECTION = '/settings/remote-access/connection';
/** The rename's frames sit at its own address; the rest at the connection page's. */
function frame(name: string, title: string, size: { width: number; height: number }, props: Partial<ConnectionPageViewProps>, extra: Partial<FrameSpec> = {}): FrameSpec {
  const route = typeof extra.route === 'string' ? extra.route : `${CONNECTION}/address`;
  return {
    name, title, ...size, status: 'shipped', themes: ['light'],
    fixtures: settingsFixtures({}),
    ...extra,
    route,
    render: settingsPage(route.replace('/settings/', ''), <ConnectionPageView {...base} {...props} />),
  };
}
const rebind = (name: string, title: string, size: { width: number; height: number }, state: RebindState, props: Partial<ConnectionPageViewProps> = {}) => (
  frame(name, title, size, { rebind: state, ...props }, { route: CONNECTION })
);

const designFrames = frames([
  // 更改域名: the form.
  frame('form', '更改域名 · 表单：后果、清不到的那一处、每台设备怎么先退出', WIDE, { rename: { step: 'form', name: 'maxwell' } }, { themes: ['light', 'dark'] }),
  frame('form-phone', '更改域名 · 表单 · 375 宽', { width: 375, height: 1500 }, { rename: { step: 'form', name: 'maxwell' } }),
  frame('form-empty', '更改域名 · 还没填名字', WIDE, { rename: { step: 'form', name: '' } }),
  frame('form-no-date', '更改域名 · avibe.bot 没给放出日期：说 30 天，不编日期', WIDE,
    { address: address({ releaseAtIfChangedNow: undefined }), rename: { step: 'form', name: 'maxwell' } }),
  frame('form-no-health', '更改域名 · 没有设备连着健康：后果清单少一条', WIDE,
    { address: address({ healthConnected: false }), rename: { step: 'form', name: 'maxwell' } }),
  frame('form-take-back', '更改域名 · 等待期内自己的旧名字：带真实日期，可以直接拿回', WIDE,
    { address: address({ retired: [studio] }), rename: { step: 'form', name: 'studio' } }),
  frame('form-taken', '更改域名 · 名字拿不到：只说已被占用，下面收起', MID, { rename: { step: 'form', name: 'max', refusal: { code: 'taken' } } }),
  frame('form-limit', '更改域名 · 账号持有的名字到上限：数字和日期来自 avibe.bot，下面收起', MID,
    { address: address({ retired: [studio] }), rename: { step: 'form', name: 'maxim', refusal: { code: 'name_holding_limit_reached', held: 6, earliestReleaseAt: '2026-12-21T00:00:00.000Z' } } }),
  frame('form-limit-phone', '到上限的拒绝 · 375 宽', PHONE,
    { address: address({ retired: [studio] }), rename: { step: 'form', name: 'maxim', refusal: { code: 'name_holding_limit_reached', held: 6, earliestReleaseAt: '2026-12-21T00:00:00.000Z' } } }),
  frame('form-mismatch', '更改域名 · 这台电脑的远程访问配置和 avibe.bot 的记录对不上：先重新绑定', MID,
    { rename: { step: 'form', name: 'maxwell', refusal: { code: 'remote_access_configuration_mismatch' } } }),
  frame('form-unreachable', '更改域名 · 连不上 avibe.bot：什么都没有改，可以再试', WIDE,
    { rename: { step: 'form', name: 'maxwell', refusal: { code: 'unavailable' } } }),
  frame('form-earlier-failed', '重新打开 · 上一次更改没有完成：先说原因', WIDE,
    { rename: { step: 'form', name: '', refusal: { code: 'taken' }, earlier: true } }),
  frame('form-unknown-code', '更改域名 · 这一页不认识的拒绝：照实说，带代码', WIDE,
    { rename: { step: 'form', name: 'maxwell', refusal: { code: 'unknown', raw: 'rename_frozen' } } }),

  // The second question, the wait, the list afterwards.
  frame('confirm', '更改域名 · 第二次确认：按钮先等几秒', MID,
    { rename: { step: 'confirm', name: 'maxwell', hostname: newHost, countdown: 3, isReturn: false, releaseAt: RELEASE, holdDays: 30, healthConnected: true, devices } }),
  frame('confirm-ready', '更改域名 · 第二次确认：可以按了', MID,
    { rename: { step: 'confirm', name: 'maxwell', hostname: newHost, countdown: 0, isReturn: false, releaseAt: RELEASE, holdDays: 30, healthConnected: true, devices } }),
  frame('confirm-phone', '第二次确认 · 375 宽', PHONE,
    { rename: { step: 'confirm', name: 'maxwell', hostname: newHost, countdown: 0, isReturn: false, releaseAt: RELEASE, holdDays: 30, healthConnected: true, devices } }),
  frame('pending', '更改域名 · avibe.bot 还没有答复：更改还在进行，这一页自己更新', MID, { rename: { step: 'pending', hostname: newHost } }),
  frame('done', '改名之后 · 清单：已清理 / 未确认 · 上次活跃；从不说全部清干净', { width: 1440, height: 1200 },
    { host: newHost, address: address({ hostname: newHost, retired: [retiredMax] }), rename: { step: 'done', hostname: newHost, previous: host, retired: retiredMax } }),
  frame('done-phone', '改名之后 · 375 宽', { width: 375, height: 1300 },
    { host: newHost, address: address({ hostname: newHost, retired: [retiredMax] }), rename: { step: 'done', hostname: newHost, previous: host, retired: retiredMax } }),
  frame('done-with-guest', '改名之后 · 清单里有别人的设备：标题不说「你的」', { width: 1440, height: 1200 },
    { host: newHost, address: address({ hostname: newHost }), rename: { step: 'done', hostname: newHost, previous: host, retired: { ...retiredMax, devices: withGuest } } }),
  frame('done-list-unread', '改名之后 · 清单暂时读不到：照实说，告诉他之后去哪看', MID,
    { host: newHost, address: address({ hostname: newHost }), rename: { step: 'done', hostname: newHost, previous: host, retired: null } }),
  frame('waiting-names', '连接方式 · 域名下面：等待中的旧名字各带真实日期，随时能再看清理情况', WIDE,
    { host: newHost, address: address({ hostname: newHost, retired: [retiredMax, studio] }) }, { route: CONNECTION }),
  frame('cleanup', '之后再看某个旧名字的清理情况', { width: 1440, height: 1200 },
    { host: newHost, address: address({ hostname: newHost, retired: [retiredMax] }), rename: { step: 'cleanup', retired: retiredMax } }, { route: CONNECTION }),
  frame('reopen-pending', '重新打开时更改还在进行：域名那一行如实说', WIDE,
    { address: address({ operation: { state: 'pending', hostname: newHost } }), rename: { step: 'pending', hostname: newHost } }),
  frame('ineligible', '不能在这里改名的 Avibe（组织的、自定义域名的）：没有「更改域名」', WIDE,
    { address: address({ eligible: false }) }, { route: CONNECTION }),

  // 重新绑定远程访问: the row, in each state the binding can be in.
  rebind('rebind', '重新绑定 · 最下面一行安静的入口（在运行 Avibe 的电脑上）', WIDE, { state: 'ready' }),
  rebind('rebind-phone', '重新绑定的那一行 · 375 宽', { width: 375, height: 1900 }, { state: 'ready' }),
  rebind('rebind-remote', '重新绑定 · 远程会话：只说要回到那台电脑上做，没有按钮', WIDE, { state: 'remote' }, { remote: true, via: 'tunnel' }),
  rebind('rebind-desktop', '重新绑定 · 在 Avibe Desktop 里：一个按钮，在这台电脑的浏览器里打开这一页继续', WIDE, { state: 'ready' }, { rebindNeedsBrowser: true }),
  rebind('rebind-working', '重新绑定 · 请求已发出', WIDE, { state: 'ready', busy: true }),
  rebind('rebind-open', '重新绑定 · 请求发出后中途回来了：说还在等 avibe.bot 上的确认，可以取消', WIDE, { state: 'ready', busy: true }, { rebindOpen: 'waiting' }),
  rebind('rebind-rejected', '重新绑定 · avibe.bot 已连续拒绝这台电脑的绑定；隧道还在跑不代表绑定被接受', WIDE, { state: 'ready', rejected: true, tunnelRunning: true }),
  rebind('rebind-replay-refused', '重新绑定 · 之前的一次绑定被彻底拒绝了', WIDE, { state: 'ready', replayRefused: true }),
  rebind('rebind-active-elsewhere', '重新绑定 · 被拒：那个 Avibe 15 分钟内还在另一台电脑上活跃', WIDE, { state: 'ready', refusal: { code: 'active_pairing_recent' } }),
  rebind('rebind-unexplained', '重新绑定 · 被拒，没有给原因：什么也不提供', WIDE, { state: 'ready', refusal: { code: 'binding_unavailable' } }),
  rebind('rebind-invalid-return', '重新绑定 · 从 avibe.bot 回来的这一步没通过验证：说清楚什么都没变，可以再试', WIDE, { state: 'ready', refusal: { code: 'invalid_return' } }),
  rebind('rebind-local-refusal', '重新绑定 · 这台电脑自己拒绝的、这一页不认识的代码：只说没有完成，带代码', WIDE,
    { state: 'asNewOffered', reason: 'predecessor_unrecoverable', refusal: { code: 'unknown', raw: 'reset_not_eligible' } }, { canReset: true }),
  rebind('rebind-recovering', '重新绑定 · 上一次没做完：继续那一次，不开始第二次', WIDE, { state: 'recovering' }),
  rebind('rebind-busy', '重新绑定 · 绑定或域名正在进行另一项更改', WIDE, { state: 'busy' }),
  rebind('rebind-resetting', '重新绑定 · 重置开始了但没做完', WIDE, { state: 'resetting' }),
  rebind('rebind-needs-terminal', '重新绑定 · 这一页处理不了的状态：一句话，指向终端命令', WIDE, { state: 'needsTerminal' }),
  rebind('rebind-unknown', '重新绑定 · 读不到绑定状态', WIDE, { state: 'unknown' }),
  rebind('rebind-as-new-offered', '被拒，原来的 Avibe 接不回来了：才提供「作为一台新电脑重新绑定」', WIDE,
    { state: 'asNewOffered', reason: 'predecessor_unrecoverable' }, { canReset: true }),
  rebind('rebind-other-account', '被拒，属于另一个账号：先说是谁的（只有打码的提示），再提供', WIDE,
    { state: 'asNewOffered', reason: 'other_account', hint: 'm***@gmail.com' }, { canReset: true }),
  rebind('rebind-other-account-phone', '属于另一个账号 · 375 宽', { width: 375, height: 2000 },
    { state: 'asNewOffered', reason: 'other_account', hint: 'm***@gmail.com' }, { canReset: true }),
  rebind('rebind-as-new-elsewhere', '可以作为新电脑重新绑定，但这次请求不是这个浏览器发起的：先在这里再点一次', WIDE,
    { state: 'asNewOffered', reason: 'predecessor_unrecoverable' }, { canReset: false }),

  // Its dialogs.
  rebind('rebind-confirm', '重新绑定 · 确认：完成后旧的设备密钥作废，地址通常不变；数据被整个复制过的话该怎么办', MID, { state: 'ready' }, { rebindDialog: { kind: 'confirm' } }),
  rebind('rebind-confirm-phone', '重新绑定 · 确认 · 375 宽', PHONE, { state: 'ready' }, { rebindDialog: { kind: 'confirm' } }),
  rebind('rebind-as-new', '作为一台新电脑重新绑定 · 确认：会失去什么、保留什么、不会改变什么', { width: 1440, height: 1200 },
    { state: 'asNewOffered', reason: 'predecessor_unrecoverable' }, { canReset: true, rebindDialog: { kind: 'asNew', healthConnected: true } }),
  rebind('rebind-as-new-other-account', '作为一台新电脑重新绑定 · 属于另一个账号：先说是谁的', { width: 1440, height: 1200 },
    { state: 'asNewOffered', reason: 'other_account', hint: 'm***@gmail.com' }, { canReset: true, rebindDialog: { kind: 'asNew', healthConnected: false } }),
  rebind('rebind-as-new-phone', '作为一台新电脑重新绑定 · 375 宽', { width: 375, height: 1500 },
    { state: 'asNewOffered', reason: 'predecessor_unrecoverable' }, { canReset: true, rebindDialog: { kind: 'asNew', healthConnected: true } }),
  rebind('rebind-reset-done', '重置完成：不再绑定；怎样把原来的名字拿回来', MID, { state: 'unpaired' },
    { host: undefined, address: null, rebindDialog: { kind: 'resetDone', oldHost: host } }),
  rebind('rebind-bound', '重新绑定完成：所有远程设备都要重新登录', MID, { state: 'ready' }, { rebindDialog: { kind: 'bound', disposition: 'existing', host } }),
  rebind('rebind-bound-new', '绑定成了一个新的 Avibe：说新地址', MID, { state: 'ready' },
    { host: newHost, address: address({ hostname: newHost }), rebindDialog: { kind: 'bound', disposition: 'new', host: newHost } }),
]);

export default designFrames;
