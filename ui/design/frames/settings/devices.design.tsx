// Settings · Devices (src/components/devices/DevicesPageView.tsx), at `/settings/remote-access`,
// where Settings · Remote access was. One page answers "which devices can open this Avibe, from
// where, and what may each do": how this Avibe is reached (set once, no pairing key), the devices
// in the lists the instance names (mine, other people's, notification devices with no known
// owner), what each offers, who was signed out and what avibe.bot did about it, and recent
// activity. Every frame draws the shipped view with the data `DevicesPage` would feed it.
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { DESIGN_NOW } from '../../shell/clock';
import { frames, type FrameSpec } from '../../frame';
import { DevicesPageView, type DevicesPageViewProps } from '@/components/devices/DevicesPageView';
import type { AccountResult, Device, DeviceActivity, DeviceFunction, Reach, SignedOutDevice } from '@/components/devices/types';
import { tunnelFromStatus } from '@/components/devices/wire';
import type { RemoteAccessStatus } from '@/context/ApiContext';
import { MovedPage, SignInNotFinishedPage } from '../../kit/devices-pages';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';
const account = 'max@example.test';
const host = 'max-app.avibe.bot';

const fixtures = (extra: WorkbenchFixtures = {}) => settingsFixtures({}, extra);

/** An instant `minutes` before the design clock. */
const ago = (minutes: number) => new Date(DESIGN_NOW - minutes * 60_000).toISOString();
const DAY = 24 * 60;

const macbook = (extra: Partial<Device> = {}): Device => ({
  id: 'host', group: 'mine', kind: 'host', name: 'Max 的 MacBook Pro', client: { kind: 'runtime', version: '3.2.8', platform: 'macOS' },
  functions: [{ kind: 'computerUse', status: 'ready' }, { kind: 'notifications', on: true, canChange: true }],
  ...extra,
});

const iphoneFunctions: DeviceFunction[] = [
  { kind: 'notifications', on: true, canChange: true },
  { kind: 'health', on: true, canChange: true, data: ['steps', 'activity', 'workouts', 'sleep'], syncedAt: ago(468) },
  { kind: 'schedule', on: true, canChange: true },
];

const iphone = (extra: Partial<Device> = {}): Device => ({
  id: 'iphone', group: 'mine', kind: 'iphone', name: 'iPhone 15 Pro', account, client: { kind: 'app', version: '3.2.8', platform: 'iOS 26.1' },
  online: { path: 'punched', family: 'ipv6', ms: 38 }, signedInAt: ago(10 * DAY),
  functions: iphoneFunctions,
  recent: [
    { id: 'r1', deviceId: 'iphone', at: ago(468), device: 'iPhone 15 Pro', event: { kind: 'healthRead', data: ['steps', 'sleep'], fromDay: '2026-11-15', toDay: '2026-11-21', task: '晨间简报' } },
    { id: 'r2', deviceId: 'iphone', at: ago(1040), device: 'iPhone 15 Pro', event: { kind: 'alarmCreated', label: '06:30 起床', confirmed: true } },
  ],
  ...extra,
});

const macMini: Device = {
  id: 'mini', group: 'mine', kind: 'desktop', name: 'Mac mini（办公室）', account, client: { kind: 'desktop', version: '3.2.8', platform: 'macOS' },
  online: { path: 'tunnel', ms: 120 }, signedInAt: ago(379), functions: [],
};
const chrome: Device = {
  id: 'chrome', group: 'mine', kind: 'browser', name: 'Chrome · Windows', account, client: { kind: 'browser' },
  lastActiveAt: ago(3 * DAY + 60), signedInAt: ago(14 * DAY), functions: [],
};
const safari: Device = {
  id: 'safari', group: 'mine', kind: 'browser', name: 'Safari · iPad', account, client: { kind: 'homeScreen' },
  lastActiveAt: ago(1125), signedInAt: ago(30 * DAY),
  functions: [{ kind: 'notifications', on: true, canChange: true }],
};
/** A browser that never reported a name: the page names its kind. */
const unnamed: Device = {
  id: 'unnamed', group: 'mine', kind: 'browser', name: null, account, client: { kind: 'browser' },
  lastActiveAt: ago(6 * DAY), signedInAt: ago(20 * DAY), functions: [],
};
const sara: Device = {
  id: 'sara', group: 'others', kind: 'browser', name: 'Chrome · macOS', client: { kind: 'browser' }, account: 'sara@example.test', person: 'sara@example.test',
  online: { path: 'tunnel' }, signedInAt: ago(358),
  // Someone else's: what it offers is listed, never switched, from here.
  functions: [{ kind: 'notifications', on: true, canChange: false }],
};
const saraPhone: Device = {
  id: 'sara-phone', group: 'others', kind: 'iphone', name: 'Sara 的 iPhone', client: { kind: 'app', version: '3.2.7' }, account: 'sara@example.test', person: 'sara@example.test',
  lastActiveAt: ago(200), signedInAt: ago(4 * DAY),
  functions: [{ kind: 'notifications', on: true, canChange: false }, { kind: 'schedule', on: false, canChange: false }],
};
/** No sign-in left, only a notification registration. */
const pushOnly: Device = {
  id: 'push-ipad', group: 'mine', kind: 'browser', name: 'Safari · iPad mini', account, client: { kind: 'homeScreen' }, pushOnly: true,
  lastActiveAt: ago(40 * DAY), functions: [{ kind: 'notifications', on: true, canChange: true }],
};
const orphan: Device = {
  id: 'orphan', group: 'unattributed', kind: 'browser', name: null, client: { kind: 'browser' }, pushOnly: true,
  lastActiveAt: ago(70 * DAY), functions: [{ kind: 'notifications', on: true, canChange: true }],
};

const activity: DeviceActivity[] = [
  { id: 'a1', deviceId: 'mini', at: ago(379), device: 'Mac mini（办公室）', event: { kind: 'signedIn' } },
  { id: 'a2', deviceId: 'iphone', at: ago(468), device: 'iPhone 15 Pro', event: { kind: 'healthRead', data: ['steps', 'sleep'], fromDay: '2026-11-15', toDay: '2026-11-21', task: '晨间简报' } },
  { id: 'a3', deviceId: 'iphone', at: ago(1040), device: 'iPhone 15 Pro', event: { kind: 'alarmCreated', label: '06:30 起床', confirmed: true } },
  { id: 'a4', at: ago(1257), device: null, event: { kind: 'signedOut', actor: 'Max 的 MacBook Pro' } },
];
const fullActivity: DeviceActivity[] = [
  ...activity,
  { id: 'a5', deviceId: 'iphone', at: ago(2 * DAY + 30), device: 'iPhone 15 Pro', event: { kind: 'reminderCreated', confirmed: false } },
  { id: 'a6', deviceId: 'sara', at: ago(3 * DAY), device: 'Chrome · macOS', event: { kind: 'signedIn' } },
  { id: 'a7', deviceId: 'iphone', at: ago(9 * DAY), device: 'iPhone 15 Pro', event: { kind: 'healthRead', data: ['sleep'], fromDay: '2026-11-12', toDay: '2026-11-12' } },
  { id: 'a8', deviceId: 'iphone', at: ago(10 * DAY + 34), device: 'iPhone 15 Pro', event: { kind: 'signedIn' } },
  { id: 'a9', deviceId: 'host', at: ago(16 * DAY), device: 'Max 的 MacBook Pro', event: { kind: 'remoteAccessOff' } },
  { id: 'a10', deviceId: 'host', at: ago(30 * DAY + 204), device: 'Max 的 MacBook Pro', event: { kind: 'remoteAccessOn' } },
];

type On = Extract<Reach, { state: 'on' }>;
/** At the computer that runs Avibe: its session carries no account, so none is shown. */
const on: On = { state: 'on', host, tunnel: 'up', ms: 120, direct: true };
/** What a member reads: whether direct is on is the owner's to see, so the line does not say it. */
const onForMember: On = { ...on, direct: false };
/** From another device of the owner's: the account is the one that device is signed in as. */
const onRemote: On = { ...on, account };
const remote = { reach: onRemote, viewerAccount: account };

/** The tunnel exactly as `/api/remote-access/status` can report it, read by the page's own mapping. */
const quality = (extra: Record<string, unknown>) => ({
  schema_version: 2, state: 'healthy', grade: 'good', sampled_at: new Date(DESIGN_NOW - 20_000).toISOString(), protocol: 'quic',
  connector_count: 1, ha_connections: 4, rtt_ms: { min: 90, median: 120, max: 160 }, baseline_median_rtt_ms: 100,
  edge_locations: [], window_seconds: 60, request_errors_per_minute: 0, packet_loss_per_minute: 0,
  recovery: { state: 'idle', last_attempt_at: null, next_attempt_at: null },
  ...extra,
});
const reported = (status: Record<string, unknown>, justStarted = false): On => {
  const tunnel = tunnelFromStatus({ ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}`, ...status } as unknown as RemoteAccessStatus, DESIGN_NOW, justStarted);
  return { ...on, tunnel: tunnel.state, ms: tunnel.ms };
};
/** The connector registered no connection: there is in fact no tunnel. */
const down = reported({ tunnel_quality: quality({ state: 'degraded', grade: 'critical', ha_connections: 0, rtt_ms: null }) });
/** Connected, with a round trip over the "very slow" line: slow, not unreachable. */
const slow = reported({ tunnel_quality: quality({ grade: 'critical', rtt_ms: { min: 380, median: 420, max: 520 } }) });
/** Connected, short of its four connections: unsteady, not unreachable. */
const unsteady = reported({ tunnel_quality: quality({ state: 'degraded', grade: 'fair', ha_connections: 2 }) });
/** A better route is being evaluated on a tunnel that is up: said as that, not as a fault. */
const optimizing = reported({ tunnel_quality: quality({ state: 'recovering', recovery: { state: 'evaluating', last_attempt_at: null, next_attempt_at: null } }) });
/** This page turned it on or bound it a moment ago: nothing read from the connector yet. */
const connecting = reported({ tunnel_quality: quality({ state: 'unknown', grade: 'unknown', ha_connections: 0, rtt_ms: null }) }, true);
/** Connected, its round trip not measured yet. */
const measuring = reported({ tunnel_quality: quality({ grade: 'unknown', ha_connections: 2, rtt_ms: null }) });
/** The connector's process cannot be read: unknown, said as unknown. */
const unreadable = reported({ running: false, pid_state: 'unknown' });
const all = (self: string): Device[] => [macbook(), iphone(), macMini, safari, chrome].map((each) => (each.id === self ? { ...each, self: true } : each));
const onlyHost = [macbook({ self: true })];
/** The list as last read, after remote access was closed from afar: no path is said any more. */
const afterOff = all('mini').map((each) => ({ ...each, online: undefined }));

const memberDevices: Device[] = [
  { ...sara, group: 'mine', person: undefined, self: true },
  { ...saraPhone, group: 'mine', person: undefined, functions: [{ kind: 'notifications', on: true, canChange: true }, { kind: 'schedule', on: true, canChange: true }] },
];

const signedOutRow = (id: string, device: string | null, results: AccountResult[], owner: SignedOutDevice['owner'] = 'unknown'): SignedOutDevice => (
  { id, device, kind: 'browser', owner, results }
);
/** One row per distinct thing the instance can say about the account sign-in. */
const signedOut: SignedOutDevice[] = [
  signedOutRow('s1', 'iPhone 14', ['queued'], 'mine'),
  signedOutRow('s2', 'Firefox · Linux', ['retrying'], 'mine'),
  signedOutRow('s3', 'Chrome · Windows', ['accepted'], 'mine'),
  signedOutRow('s4', 'Safari · iPad', ['localOnly'], 'mine'),
  signedOutRow('s5', 'Chrome · macOS', ['localOnly'], 'others'),
  signedOutRow('s6', null, ['localOnly']),
  signedOutRow('s7', 'Edge · Windows', ['sharedSignIn'], 'mine'),
  signedOutRow('s8', 'Arc · macOS', ['keeperUnknown'], 'mine'),
  signedOutRow('s9', 'iPad Air', ['refusedCredential'], 'mine'),
  signedOutRow('s10', 'Pixel 8', ['refused'], 'mine'),
  signedOutRow('s11', 'Galaxy S24', ['unconfirmed'], 'mine'),
  signedOutRow('s12', 'Vivaldi · Linux', ['unknown']),
  signedOutRow('s13', 'Mac mini（客厅）', ['accepted', 'localOnly'], 'mine'),
];

const at = (height: number) => ({ width: 1440, height });
const TALL = at(1000);
const PHONE = { width: 375, height: 820 };

function frame(
  name: string,
  title: string,
  size: { width: number; height: number; safeArea?: { top: number; bottom: number } },
  props: DevicesPageViewProps,
  extra: Partial<FrameSpec> & { app?: boolean } = {},
): FrameSpec {
  const { app, ...spec } = extra;
  return {
    name,
    title,
    ...size,
    status: 'shipped',
    themes: ['light'],
    route,
    fixtures: fixtures(app ? { nativeShell: IOS_APP_CAPABILITIES } : {}),
    render: settingsPage('remote-access', <DevicesPageView {...props} />),
    ...spec,
  };
}

const designFrames = frames([
  // 1. Remote access off: one action, no key.
  frame('off', '未开启 · 浏览器：一个动作，去 avibe.bot 起名后自动回来', at(700),
    { reach: { state: 'off', surface: 'browser' }, devices: onlyHost }),
  frame('off-organization', '未开启 · 这台电脑自己的记录说它是组织的 Avibe：不提供个人的开启，一句话', at(700),
    { reach: { state: 'off', surface: 'browser', problem: { code: 'organization' } }, devices: onlyHost }),
  frame('off-phone', '未开启 · 375 宽', PHONE, { reach: { state: 'off', surface: 'browser' }, devices: onlyHost }),
  frame('off-sending', '未开启 · 点了开启，正在向本机要这次请求', at(700),
    { reach: { state: 'off', surface: 'browser', busy: true }, devices: onlyHost }),
  frame('off-link-required', '未开启 · 从隧道或代理地址打开：这里开不了，一句话说去哪开', at(700),
    { reach: { state: 'off', surface: 'browser', problem: { code: 'link_required' } }, devices: onlyHost }),
  frame('off-link-required-no-terminal', '未开启 · 同上，在还没有 vibe remote rebind 的版本上：不提终端命令', at(700),
    { reach: { state: 'off', surface: 'browser', problem: { code: 'link_required' } }, devices: onlyHost, rebindOffered: false }),
  frame('off-claim-in-progress', '未开启 · 另一个标签页已有请求：先完成或取消那一个', at(700),
    { reach: { state: 'off', surface: 'browser', problem: { code: 'claim_in_progress', claimId: 'c1' } }, devices: onlyHost }),
  frame('off-invalid-return', '未开启 · 从 avibe.bot 回来的这一步没有通过验证', at(700),
    { reach: { state: 'off', surface: 'browser', problem: { code: 'invalid_return' } }, devices: onlyHost }),
  frame('off-active-elsewhere', '未开启 · avibe.bot 拒绝：那个 Avibe 15 分钟内还在另一台电脑上活跃', at(760),
    { reach: { state: 'off', surface: 'browser', problem: { code: 'active_pairing_recent' } }, devices: onlyHost }),
  frame('off-unknown-code', '未开启 · 这一页不认识的拒绝：照实说，带代码', at(700),
    { reach: { state: 'off', surface: 'browser', problem: { code: 'unknown', raw: 'local_claim_unavailable' } }, devices: onlyHost }),
  frame('off-unreachable', '未开启 · 点了开启，但连不上 avibe.bot', at(700),
    { reach: { state: 'off', surface: 'browser', problem: { code: 'unreachable' } }, devices: onlyHost }),
  frame('off-desktop', '未开启 · 在 Avibe Desktop 里：一个动作，请壳打开认领弹窗（App 自己的登录、起名）', at(700),
    { reach: { state: 'off', surface: 'desktop' }, devices: onlyHost }),
  frame('waiting', '请求已发出，avibe.bot 上还没有确认：可以取消', at(640),
    { reach: { state: 'starting', waiting: true }, devices: onlyHost }),
  frame('retrying', '请求还开着，实例暂时连不上 avibe.bot、在重试：不说「等待确认」', at(640),
    { reach: { state: 'starting', retrying: true }, devices: onlyHost }),
  frame('list-unknown', '设备列表读不到：说读不到，不说「还没有设备」', at(640), { reach: on, devices: [], list: 'unknown' }),
  frame('starting', '从 avibe.bot 自动回来：正在开启', at(640), { reach: { state: 'starting' }, devices: onlyHost }),
  frame('loading', '第一次读状态', at(600), { reach: { state: 'loading' }, devices: onlyHost }),
  frame('unknown', '读不到远程访问的状态：照实说，可以重试', at(640), { reach: { state: 'unknown' }, devices: onlyHost }),

  // 2. Bound from a private-network address: this browser is no longer this computer.
  frame('moved', '从局域网另一台电脑绑定成功：请打开新地址（本机接口说过新地址）', at(640),
    { reach: { state: 'moved', host }, devices: [] }, { themes: ['light', 'dark'] }),
  frame('moved-phone', '请打开新地址 · 375 宽', { width: 375, height: 700 }, { reach: { state: 'moved', host }, devices: [] }),
  frame('moved-unknown', '请打开新地址 · 读不到新地址：链接指向 avibe.bot', at(640), { reach: { state: 'moved' }, devices: [] }),
  frame('moved-unknown-phone', '请打开新地址 · 读不到新地址 · 375 宽', { width: 375, height: 700 }, { reach: { state: 'moved' }, devices: [] }),

  // 3. On, with devices.
  // The account under the domain is the signed-in session's own: it shows where the owner opens
  // the page from a signed-in device, and not at the computer itself, which has no sign-in.
  frame('on', '已开启 · 在运行 Avibe 的电脑上看（这里没有登录账号，所以域名下面不显示账号）：主机、iPhone（打洞直连）、另一台电脑（Cloudflare 隧道）、两个浏览器', TALL,
    { reach: on, devices: all('host'), activity, bulk: 'all_remote' }, { themes: ['light', 'dark'] }),
  frame('on-phone', '已开启 · 所有者在 iPhone App 里看（已登录，所以域名下面是这次登录的账号）', { ...IPHONE_17_PRO, height: 1240 },
    { ...remote, devices: all('iphone'), activity, bulk: 'other_sessions' }, { app: true }),
  frame('on-remote', '已开启 · 从另一台电脑远程看：可以退出我的其他设备', TALL,
    { ...remote, devices: all('mini'), activity, bulk: 'other_sessions' }),
  frame('on-shared', '已开启 · 有别人的设备、认不出主人的通知设备、只接收通知的设备：各在自己的一组', at(1500),
    { reach: on, devices: [...all('host'), pushOnly, unnamed, sara, saraPhone, orphan], activity, bulk: 'all_remote' }),
  frame('on-shared-phone', '三组设备 · 375 宽', { width: 375, height: 1900 },
    { reach: on, devices: [...all('host'), pushOnly, unnamed, sara, saraPhone, orphan], activity, bulk: 'all_remote' }),
  frame('member-operate', '能管理这个 Avibe 的成员：自己的设备，加上远程访问的开关和「优化线路」；绑定、域名、连接方式、别人的设备都没有', at(760),
    { reach: onForMember, viewer: 'member', operate: true, viewerAccount: 'sara@example.test', bulk: 'other_sessions', devices: memberDevices }),
  frame('member-operate-phone', '能管理的成员 · 375 宽', PHONE,
    { reach: onForMember, viewer: 'member', operate: true, viewerAccount: 'sara@example.test', bulk: 'other_sessions', devices: memberDevices }),
  frame('member-operate-waiting', '能管理的成员 · 「优化线路」下面一行：自动优化什么时候重试', at(760),
    { reach: onForMember, viewer: 'member', operate: true, viewerAccount: 'sara@example.test', devices: memberDevices, optimize: { wait: { kind: 'automatic', time: '10:42:00' } } }),
  frame('member-operate-turn-off', '能管理的成员从远处关闭远程访问：同样先确认', at(860),
    { reach: onForMember, viewer: 'member', operate: true, viewerAccount: 'sara@example.test', devices: memberDevices, confirm: 'turn-off' }),
  frame('member-operate-paused', '能管理的成员 · 远程访问关着（在本机网络里看）：可以再打开', at(700),
    { reach: { state: 'paused', host }, viewer: 'member', operate: true, viewerAccount: 'sara@example.test', devices: memberDevices }),
  frame('member', '不能管理的成员：只看到自己的设备，没有实例的控制项', at(640),
    { reach: onForMember, viewer: 'member', viewerAccount: 'sara@example.test', bulk: 'other_sessions',
      devices: memberDevices }),
  frame('tunnel-unknown', '已开启 · 连接器的进程读不出来：说读不到，不说连不上', at(700), { reach: unreadable, devices: all('host') }),
  frame('tunnel-slow', '已开启 · 隧道连着但很慢（延迟过了「很慢」那条线）：说慢，不说连不上', at(760), { reach: slow, devices: all('host') }),
  frame('tunnel-slow-phone', '隧道慢 · 375 宽', PHONE, { reach: slow, devices: all('host') }),
  frame('tunnel-unsteady', '已开启 · 隧道连着但四条连接没连满：说不太稳定', at(760), { reach: unsteady, devices: all('host') }),
  frame('tunnel-optimizing', '已开启 · 正在优化线路：隧道连着，照实说在优化', at(760), { reach: optimizing, devices: all('host') }),
  frame('tunnel-connecting', '已开启 · 这一页刚开启或刚绑定（一分钟内），连接器还没读到：说正在连接，不说连不上、不给重试', at(760), { reach: connecting, devices: all('host') }),
  frame('tunnel-measuring', '已开启 · 连上了，延迟还没测出来：说已连接，不带数字，不说读不到', at(760), { reach: measuring, devices: all('host') }),
  frame('tunnel-down', '已开启 · 连接器一条连接都没有登记上：这才是连不上 Cloudflare', at(760),
    { reach: down, devices: [macbook({ self: true }), iphone({ online: undefined, lastActiveAt: ago(12) }), { ...macMini, online: undefined, lastActiveAt: ago(12) }] }),
  frame('tunnel-down-phone', '连不上 Cloudflare · 375 宽：域名完整显示，按钮和开关换到下一行', PHONE,
    { reach: down, devices: [macbook({ self: true }), iphone({ online: undefined, lastActiveAt: ago(12) }), { ...macMini, online: undefined, lastActiveAt: ago(12) }] }),
  frame('paused', '关闭后：域名保留，一个开关就能再开', at(700),
    { reach: { state: 'paused', host }, devices: [macbook({ self: true }), iphone({ online: undefined, lastActiveAt: ago(120) })] }),

  // 4. A device's detail.
  frame('detail-iphone', '设备详情 · iPhone：连接、三项功能各一个开关、最近、退出这台设备', TALL,
    { reach: on, devices: all('host'), activity, detail: 'iphone' }),
  frame('detail-iphone-phone', '设备详情 · 手机宽度（底部面板）', { ...IPHONE_17_PRO, height: 900 },
    { ...remote, devices: all('safari'), activity, detail: 'iphone' }),
  frame('detail-health-off', '设备详情 · 健康没有连接：只说状态和去哪连，没有开关', TALL,
    { reach: on, activity, detail: 'iphone', devices: [macbook({ self: true }), iphone({ functions: [iphoneFunctions[0], { kind: 'health', on: false, canChange: false, data: [] }, iphoneFunctions[2]] })] }),
  frame('detail-browser', '设备详情 · 浏览器：没有会丢的东西，退出不需要确认', at(860),
    { reach: on, devices: all('host'), activity, detail: 'chrome' }),
  frame('detail-host', '设备详情 · 运行 Avibe 的电脑：没有「退出」；电脑操作只读', at(860),
    { reach: on, devices: all('host'), activity, detail: 'host' }),
  frame('detail-host-needs-permission', '设备详情 · 从别的设备看主机：电脑操作需要授权', at(860),
    { ...remote, activity, detail: 'host', devices: all('mini').map((each) => (each.id === 'host'
      ? macbook({ functions: [{ kind: 'computerUse', status: 'needs_permission' }, { kind: 'notifications', on: true, canChange: false }] })
      : each)) }),
  frame('detail-other-person', '设备详情 · 别人的设备：功能只读，只结束这次访问', at(1080),
    { reach: on, devices: [...all('host'), sara, saraPhone], activity, detail: 'sara-phone' }),
  frame('detail-push-only', '设备详情 · 只接收通知、没有登录的设备：停止通知', at(900),
    { reach: on, devices: [...all('host'), pushOnly], activity, detail: 'push-ipad' }),

  // 5. Confirmations: asked only where something is lost.
  frame('confirm-sign-out-health', '在运行 Avibe 的电脑上退出 iPhone：健康数据要确认所有者之后才删，照实说，按钮只是「退出」', at(860),
    { reach: on, devices: all('host'), activity, detail: 'iphone', confirm: 'remove' }),
  frame('confirm-sign-out-health-remote', '从别的设备上退出 iPhone：会删除健康数据，所以先确认', at(860),
    { ...remote, devices: all('mini'), activity, detail: 'iphone', confirm: 'remove' }),
  frame('confirm-health-off', '关闭 iPhone 的健康：会删除已同步的数据，所以先确认', at(860),
    { reach: on, devices: all('host'), activity, detail: 'iphone', confirm: 'health-off' }),
  frame('confirm-health-off-phone', '关闭健康 · 375 宽', PHONE, { reach: on, devices: all('host'), activity, detail: 'iphone', confirm: 'health-off' }),
  frame('confirm-turn-off-remote', '远程关闭远程访问：会把自己关在外面，所以先确认', at(860),
    { ...remote, devices: all('mini'), activity, confirm: 'turn-off' }),
  frame('confirm-sign-out-others', '退出其他所有设备：不包括发出请求的这一台', at(860),
    { ...remote, devices: all('mini'), activity, bulk: 'other_sessions', confirm: 'remove-others' }),
  frame('confirm-sign-out-all-remote', '在运行 Avibe 的电脑上退出所有远程设备：包括别人的', at(860),
    { reach: on, devices: [...all('host'), sara], activity, bulk: 'all_remote', confirm: 'remove-all-remote' }),

  // 6. Signing out: what avibe.bot did about the account sign-in. Never 「已结束」.
  frame('sign-out-queued', '刚退出 · 已排队；这一行显示过健康，所以健康清理说「尚未确认」', TALL,
    { reach: on, devices: all('host'), activity, signOutNotice: { device: 'iPhone 14', owner: 'mine', results: ['queued'], healthCleanupPending: true, hadHealth: true } }),
  frame('sign-out-local-mine', '刚退出我自己的一台 · 只在这个 Avibe 上退出了：只说对自己设备成立的那一半', TALL,
    { reach: on, devices: all('host'), activity, signOutNotice: { device: 'Safari · iPad', owner: 'mine', results: ['localOnly'], healthCleanupPending: true, hadHealth: false } }),
  frame('sign-out-local-others', '刚退出别人的一台 · 只说对别人设备成立的那一半', TALL,
    { reach: on, devices: [...all('host'), sara], activity, signOutNotice: { device: 'Chrome · macOS', owner: 'others', results: ['localOnly'], healthCleanupPending: false, hadHealth: false } }),
  frame('sign-out-many', '刚退出所有远程设备 · 几种结果各一句，不知道是谁的就两半都说', at(1100),
    { reach: on, devices: onlyHost, activity, signOutNotice: { device: null, owner: 'unknown', results: ['accepted', 'localOnly', 'retrying'], healthCleanupPending: false, hadHealth: false } }),
  frame('sign-out-many-phone', '刚退出所有远程设备 · 375 宽', { width: 375, height: 1300 },
    { reach: on, devices: onlyHost, activity, signOutNotice: { device: null, owner: 'unknown', results: ['accepted', 'localOnly', 'retrying'], healthCleanupPending: false, hadHealth: false } }),
  frame('signed-out', '最近退出的设备：接口能说出的每一种结果各一行', at(2100),
    { reach: on, devices: all('host'), signedOut }),
  frame('signed-out-phone', '最近退出的设备 · 375 宽', { width: 375, height: 3300 }, { reach: on, devices: onlyHost, signedOut }),

  // 7. Health connections no listed device is proven to own; things that did not happen.
  frame('unlinked-health', '未对应到设备的健康连接：各自一行，按自己的编号断开', at(1100),
    { reach: on, devices: all('host'), activity, unlinkedHealth: [
      { id: 'p1', device: 'iPhone 13 mini', data: ['steps', 'sleep'], lastSeenAt: ago(9 * DAY) },
      { id: 'p2', device: '', data: [], lastSeenAt: undefined },
    ] }),
  frame('unlinked-health-confirm', '断开一条未对应的健康连接：会删除它同步的数据，所以先确认', at(1100),
    { reach: on, devices: all('host'), activity, confirm: { unlinkedHealth: 'p1' }, unlinkedHealth: [{ id: 'p1', device: 'iPhone 13 mini', data: ['steps', 'sleep'], lastSeenAt: ago(9 * DAY) }] }),
  frame('problem-function', '开关没有改成：要先在那台设备上打开这一项', TALL,
    { reach: on, devices: all('host'), activity, problem: 'functionNeedsDevice' }),
  frame('problem-gone', '要操作的设备已经不在列表里', TALL, { reach: on, devices: all('host'), activity, problem: 'gone' }),
  frame('problem-lost', '退出或断开的应答丢了：结果不知道，重新读过了，不说失败', TALL, { ...remote, devices: all('mini'), activity, problem: 'lost' }),
  frame('off-from-afar', '从远处关闭远程访问之后：收不到回答正是关成功的样子，照实这样说', at(760),
    { reach: { state: 'unknown' }, viewerAccount: account, devices: afterOff, problem: 'offFromAfar' }),
  frame('off-from-afar-phone', '从远处关闭之后 · 375 宽', PHONE,
    { reach: { state: 'unknown' }, viewerAccount: account, devices: afterOff, problem: 'offFromAfar' }),
  frame('closed-from-afar', '从远处关闭、关闭的请求说已完成，之后读不到：说已关闭', at(760),
    { reach: { state: 'unknown' }, viewerAccount: account, devices: afterOff, problem: 'closedFromAfar' }),
  frame('closed-from-afar-phone', '从远处关闭、说已完成之后 · 375 宽', PHONE,
    { reach: { state: 'unknown' }, viewerAccount: account, devices: afterOff, problem: 'closedFromAfar' }),
  frame('unknown-kind', '一种这一页还不认识的设备：照样列出来，叫「设备」', at(760),
    { reach: on, devices: [...all('host'), { id: 'watch', group: 'mine', kind: 'other', name: null, client: { kind: 'other' }, lastActiveAt: ago(90), functions: [] }] }),
  // While 「更改域名」 and binding again are not in the release (features.ts), the notice says the fact
  // and sends the owner to support; once both are, it says rebind and take the name back.
  frame('outside-change', '地址在这台 Avibe 之外被改了：醒目提示，说事实，请联系 support@avibe.bot（重新绑定和更改域名上线后换回原来那句）', at(1100),
    { reach: { ...on, host: 'sam-app.avibe.bot' }, devices: all('host'), activity, unrecognizedChange: { hostname: 'sam-app.avibe.bot' } }),
  frame('outside-change-phone', '地址在别处被改了 · 375 宽', { width: 375, height: 1500 },
    { reach: { ...on, host: 'sam-app.avibe.bot' }, devices: all('host'), activity, unrecognizedChange: { hostname: 'sam-app.avibe.bot' } }),

  // 8. Adding a device.
  frame('empty', '刚开启，只有主机：「添加设备」直接摆在列表里（iPhone）', at(760), { reach: on, devices: onlyHost }),
  frame('empty-phone', '只有主机 · 375 宽', { width: 375, height: 1000 }, { reach: on, devices: onlyHost }),
  frame('empty-android', '添加设备 · 安卓手机：没有 App，如实给网页加主屏幕', at(760), { reach: on, devices: onlyHost, add: 'android' }),
  frame('empty-computer', '添加设备 · 另一台电脑：Desktop App 下载页，或直接用浏览器', at(760), { reach: on, devices: onlyHost, add: 'computer' }),
  frame('add', '已有其他设备后：标题旁一个安静的「添加设备」，点开是同一块内容', TALL,
    { reach: on, devices: all('host'), activity, add: 'iphone' }),

  // 9. Activity.
  frame('activity', '活动：登录、退出和功能使用，新的在上；已不在列表里的设备照实说', at(960),
    { reach: on, devices: all('host'), activity: fullActivity, view: 'activity' }),
  frame('activity-more', '活动 · 实例里还有更早的：一页一页往前读', at(1000),
    { reach: on, devices: all('host'), activity: fullActivity, view: 'activity', moreActivity: 'available' }),
  frame('activity-phone', '活动 · 375 宽', { width: 375, height: 1100 }, { reach: on, devices: all('host'), activity: fullActivity, view: 'activity' }),
  frame('activity-empty', '活动：还没有', at(500), { reach: on, devices: all('host'), activity: [], view: 'activity' }),

  // 10. Whole pages the instance itself answers with (plain HTML from the Runtime): the design record.
  { name: 'page-moved', title: '旧地址 · 等待期内打开：清掉本机数据后，一句话加 avibe.bot 的链接（实例直接返回的页面）', width: 1440, height: 800, status: 'proposed', themes: ['light'],
    fixtures: fixtures(), render: () => <MovedPage /> },
  { name: 'page-signed-out', title: '被退出的设备在撤销完成前回来：「这台设备已被退出」（实例直接返回的页面）', width: 1440, height: 800, status: 'proposed', themes: ['light'],
    fixtures: fixtures(), render: () => <SignInNotFinishedPage /> },
]);

export default designFrames;
