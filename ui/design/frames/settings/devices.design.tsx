// Settings · Devices (src/components/devices/DevicesPageView.tsx) takes the place of Settings ·
// Remote access. One page answers "which devices can open this Avibe, from where, and what may each
// do": how this Avibe is reached (set once, no pairing key), the devices (one row each; a detail
// with its functions and one way out), and recent activity. The view is not routed yet; the frames
// draw it at the route it will take over. The section's new name is proposed copy (`copy`) until
// the page is routed.
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { DESIGN_NOW } from '../../shell/clock';
import { frames, type FrameSpec } from '../../frame';
import { DevicesPageView, type DevicesPageViewProps } from '@/components/devices/DevicesPageView';
import type { Device, DeviceActivity, DeviceCapability, Reach } from '@/components/devices/types';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';
const account = 'max@example.test';
const host = 'max-app.avibe.bot';

// The section keeps its place in Settings and takes the new name.
const copy = { 'settings.sections.remoteAccess': { zh: '设备', en: 'Devices' } };
const fixtures = (extra: WorkbenchFixtures = {}) => settingsFixtures({}, { copy, ...extra });

/** An instant `minutes` before the design clock. */
const ago = (minutes: number) => new Date(DESIGN_NOW - minutes * 60_000).toISOString();
const DAY = 24 * 60;

const macbook = (extra: Partial<Device> = {}): Device => ({
  id: 'host', kind: 'host', name: 'Max 的 MacBook Pro', client: { kind: 'runtime', version: '3.2.8', platform: 'macOS' }, online: {},
  capabilities: [{ kind: 'computerUse', on: true }, { kind: 'notifications', on: true }],
  ...extra,
});

const iphoneCapabilities: DeviceCapability[] = [
  { kind: 'notifications', on: true },
  { kind: 'health', on: true, data: ['steps', 'activity', 'workouts', 'sleep'], syncedAt: ago(468) },
  { kind: 'schedule', on: true },
];

const iphone = (extra: Partial<Device> = {}): Device => ({
  id: 'iphone', kind: 'iphone', name: 'iPhone 15 Pro', client: { kind: 'app', version: '3.2.8', platform: 'iOS 26.1' },
  online: { path: 'punched', family: 'ipv6', ms: 38 }, signedInAt: ago(10 * DAY),
  capabilities: iphoneCapabilities,
  recent: [
    { id: 'r1', at: ago(468), event: { kind: 'healthRead', days: 7, data: ['steps', 'sleep'], task: '晨间简报' } },
    { id: 'r2', at: ago(1040), event: { kind: 'alarmCreated', label: '06:30 起床', confirmedOn: 'iPhone' } },
  ],
  ...extra,
});

const macMini: Device = {
  id: 'mini', kind: 'desktop', name: 'Mac mini（办公室）', client: { kind: 'desktop', version: '3.2.8', platform: 'macOS' },
  online: { path: 'tunnel', ms: 120 }, signedInAt: ago(379), capabilities: [],
};
const chrome: Device = {
  id: 'chrome', kind: 'browser', name: 'Chrome · Windows', client: { kind: 'browser' },
  lastActiveAt: ago(3 * DAY + 60), signedInAt: ago(14 * DAY), capabilities: [],
};
const safari: Device = {
  id: 'safari', kind: 'browser', name: 'Safari · iPad', client: { kind: 'homeScreen' },
  lastActiveAt: ago(1125), signedInAt: ago(30 * DAY),
  capabilities: [{ kind: 'notifications', on: true }],
};
const sara: Device = {
  id: 'sara', kind: 'browser', name: 'Chrome · macOS', client: { kind: 'browser' }, person: 'sara@example.test',
  online: { path: 'tunnel' }, signedInAt: ago(358), capabilities: [],
};

const activity: DeviceActivity[] = [
  { id: 'a1', at: ago(379), event: { kind: 'signedIn', device: 'Mac mini（办公室）' }, source: 'Avibe Desktop' },
  { id: 'a2', at: ago(468), event: { kind: 'healthRead', days: 7, data: ['steps', 'sleep'], task: '晨间简报' }, source: 'iPhone 15 Pro' },
  { id: 'a3', at: ago(1040), event: { kind: 'alarmCreated', label: '06:30 起床', confirmedOn: 'iPhone' }, source: 'iPhone 15 Pro' },
  { id: 'a4', at: ago(1257), event: { kind: 'signedOut', device: 'Firefox · Linux' }, source: 'Max 的 MacBook Pro' },
];
const fullActivity: DeviceActivity[] = [
  ...activity,
  { id: 'a5', at: ago(14 * DAY + 100), event: { kind: 'signedIn', device: 'Chrome · Windows' }, source: 'Chrome · Windows' },
  { id: 'a6', at: ago(16 * DAY + 200), event: { kind: 'directOn' }, source: 'Max 的 MacBook Pro' },
  { id: 'a7', at: ago(10 * DAY + 30), event: { kind: 'healthConnected', device: 'iPhone 15 Pro', data: ['steps', 'activity', 'workouts', 'sleep'] }, source: 'iPhone 15 Pro' },
  { id: 'a8', at: ago(10 * DAY + 34), event: { kind: 'signedIn', device: 'iPhone 15 Pro' }, source: 'Avibe App' },
  { id: 'a9', at: ago(30 * DAY + 200), event: { kind: 'signedIn', device: 'Safari · iPad' }, source: 'Safari · iPad' },
  { id: 'a10', at: ago(30 * DAY + 204), event: { kind: 'remoteAccessOn', host }, source: 'Max 的 MacBook Pro' },
];

const on: Reach = { state: 'on', host, account, tunnel: 'good', ms: 120, direct: true };
const all = (self: string): Device[] => [macbook(), iphone(), macMini, safari, chrome].map((each) => (each.id === self ? { ...each, self: true } : each));

const at = (height: number) => ({ width: 1440, height });
const TALL = at(1000);

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
    render: settingsPage('remote-access', <DevicesPageView ownerEmail={account} {...props} />),
    ...spec,
  };
}

const designFrames = frames([
  // 1. Remote access off: one action, no key.
  frame('off-browser', '未开启 · 浏览器打开的独立 Runtime：一个动作，去 avibe.bot 起名后自动回来', at(640),
    { reach: { state: 'off', surface: 'browser' }, devices: [macbook({ self: true })] }),
  frame('off-desktop-signed-in', '未开启 · Desktop App 已登录：就在这里起名，一个动作', at(760),
    { reach: { state: 'off', surface: 'desktop', account, name: 'max', nameState: 'free' }, devices: [macbook({ self: true })] }),
  frame('off-desktop-signed-out', '未开启 · Desktop App 未登录：走 App 自己的登录，回到这里', at(640),
    { reach: { state: 'off', surface: 'desktop', account: null, name: '' }, devices: [macbook({ self: true })] }),
  frame('off-desktop-name-taken', '未开启 · Desktop App：名称已被别人占用，只提示，由用户自己改', at(760),
    { reach: { state: 'off', surface: 'desktop', account, name: 'max', nameState: 'taken' }, devices: [macbook({ self: true })] }),
  frame('off-desktop-second-computer', '未开启 · 同一账号的第二台电脑：前缀已被自己的第一个 Avibe 占着，预填「前缀-电脑名」', at(760),
    { reach: { state: 'off', surface: 'desktop', account, name: 'max-macmini', nameState: 'free' }, devices: [{ ...macbook({ self: true }), name: 'Mac mini（办公室）' }] }),
  frame('starting', '从 avibe.bot 自动回来：正在开启', at(600),
    { reach: { state: 'starting', host, account }, devices: [macbook({ self: true })] }),

  // 3. On, with devices.
  frame('on', '已开启 · 在运行 Avibe 的电脑上看：主机、iPhone（打洞直连）、另一台电脑的 Desktop App（Cloudflare 隧道）、两个浏览器', TALL,
    { reach: on, devices: all('host'), activity }, { themes: ['light', 'dark'] }),
  frame('on-phone', '已开启 · 在 iPhone App 里看', { ...IPHONE_17_PRO, height: 1240 },
    { reach: on, devices: all('iphone'), activity }, { app: true }),
  frame('on-shared', '已开启 · 共享实例的所有者：别人的设备单独一组', at(1180),
    { reach: on, devices: [...all('host'), sara], activity }),
  frame('member', '共享实例的成员：只看到自己的设备', at(600),
    { reach: on, viewer: 'member', devices: [{ ...sara, person: undefined, self: true }, iphone({ id: 'sara-phone', name: 'iPhone 16', online: undefined, lastActiveAt: ago(1300), capabilities: [iphoneCapabilities[0]] })] }),

  // 4. A device's detail.
  frame('detail-iphone', '设备详情 · iPhone：连接、三项功能各一个开关、最近、退出这台设备', TALL,
    { reach: on, devices: all('host'), activity, detail: 'iphone' }),
  frame('detail-iphone-phone', '设备详情 · 手机宽度（底部面板）', { ...IPHONE_17_PRO, height: 900 },
    { reach: on, devices: all('safari'), activity, detail: 'iphone' }),
  frame('detail-confirm', '退出 iPhone：会删除健康数据，所以先确认', at(860),
    { reach: on, devices: all('host'), activity, detail: 'iphone', confirm: 'remove' }),
  frame('detail-browser', '设备详情 · 浏览器：没有会丢的东西，退出不需要确认', at(860),
    { reach: on, devices: all('host'), activity, detail: 'chrome' }),
  frame('detail-host', '设备详情 · 运行 Avibe 的电脑：没有「退出」', at(860),
    { reach: on, devices: all('host'), activity, detail: 'host' }),
  frame('detail-host-remote', '设备详情 · 从别的设备看主机：电脑操作只能在那台电脑上改', at(860),
    { reach: on, activity, detail: 'host', devices: all('mini').map((each) => (each.id === 'host'
      ? macbook({ capabilities: [{ kind: 'computerUse', on: true, fixed: true }, { kind: 'notifications', on: true }] })
      : each)) }),
  frame('detail-other-person', '设备详情 · 别人的设备：只结束这次访问', at(1080),
    { reach: on, devices: [...all('host'), sara], activity, detail: 'sara' }),

  // 5. Activity.
  frame('activity', '活动：登录、退出和功能使用，按时间', at(860),
    { reach: on, devices: all('host'), activity: fullActivity, view: 'activity' }),

  // 6. Empty and error states that really occur.
  frame('empty', '刚开启，只有主机：「添加设备」直接摆在列表里（iPhone）', at(760),
    { reach: on, devices: [macbook({ self: true })] }),
  frame('empty-android', '添加设备 · 安卓手机：没有 App，如实给网页加主屏幕', at(760),
    { reach: on, devices: [macbook({ self: true })], add: 'android' }),
  frame('empty-computer', '添加设备 · 另一台电脑：Desktop App 下载页，或直接用浏览器', at(760),
    { reach: on, devices: [macbook({ self: true })], add: 'computer' }),
  frame('add', '已有其他设备后：标题旁一个安静的「添加设备」，点开是同一块内容', TALL,
    { reach: on, devices: all('host'), activity, add: 'iphone' }),
  frame('tunnel-down', '已开启，但连不上 Cloudflare', at(760),
    { reach: { ...on, tunnel: 'down' }, devices: [macbook({ self: true }), iphone({ online: undefined, lastActiveAt: ago(12) }), { ...macMini, online: undefined, lastActiveAt: ago(12) }] }),
  frame('paused', '关闭后：域名保留，一个开关就能再开', at(700),
    { reach: { state: 'paused', host, account }, devices: [macbook({ self: true }), iphone({ online: undefined, lastActiveAt: ago(120) })] }),
  frame('turn-off-remote', '远程关闭远程访问：会把自己关在外面，所以先确认', at(860),
    { reach: on, devices: all('mini'), activity, confirm: 'turn-off' }),
  frame('remove-others', '退出其他所有设备：确认', at(860),
    { reach: on, devices: all('host'), activity, confirm: 'remove-others' }),
  frame('conflict', '从 avibe.bot 回来：这台电脑已属于另一个账号', at(660),
    { reach: { state: 'conflict', host: 'studio-app.avibe.bot', owner: 'm***@gmail.com', account }, devices: [macbook({ self: true })] }),
  frame('unreachable', '点了开启，但连不上 avibe.bot', at(700),
    { reach: { state: 'off', surface: 'browser', error: 'unreachable' }, devices: [macbook({ self: true })] }),
]);

export default designFrames;
