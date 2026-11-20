// Proposed 2026-11-20 (frames only, a draft for the owner to discuss; no implementation):
// Settings · Devices takes the place of Settings · Remote access. One page answers "which devices
// can open this Avibe, from where, and what may each do": how this Avibe is reached (set once, no
// pairing key), the devices (one row each; a detail with its functions and one way out), and recent
// activity. Facts, reasons and open questions: the Show Page of the Session that drew these.
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { frames, type FrameSpec } from '../../frame';
import { DevicesPage, type Activity, type Device, type DevicesPageProps, type Reach } from '../../kit/devices';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';
const account = 'max@example.test';
const host = 'max-app.avibe.bot';

// The section keeps its place in Settings and takes the new name.
const copy = { 'settings.sections.remoteAccess': { zh: '设备', en: 'Devices' } };
const fixtures = (extra: WorkbenchFixtures = {}) => settingsFixtures({}, { copy, ...extra });

const t = (zh: string, en: string) => ({ zh, en });

const macbook = (extra: Partial<Device> = {}): Device => ({
  id: 'host', kind: 'host', name: 'Max 的 MacBook Pro', app: 'Avibe 3.2.8 · macOS', online: {},
  capabilities: [
    { kind: 'computerUse', on: true, detail: t('Agent 可以查看并操作这台 Mac。', 'Agents can see and operate this Mac.') },
    { kind: 'notifications', on: true, detail: t('Vault 审批和后台任务完成时的系统通知。', 'System notifications for Vault approvals and finished background work.') },
  ],
  ...extra,
});

const iphone = (extra: Partial<Device> = {}): Device => ({
  id: 'iphone', kind: 'iphone', name: 'Max 的 iPhone', app: 'Avibe App 3.2.8 · iOS 26.1',
  online: { path: 'punched', ms: 38 }, signedIn: t('11 月 12 日', 'Nov 12'),
  capabilities: [
    { kind: 'notifications', on: true, detail: t('伙伴和会话的消息会推送到这台 iPhone。', 'Messages from Bots and Sessions are pushed to this iPhone.') },
    { kind: 'health', on: true, detail: t('步数、活动、锻炼、睡眠 · 今天 08:12 同步', 'Steps, activity, workouts, sleep · synced today 08:12') },
    { kind: 'schedule', on: true, detail: t('由 Agent 提出，每次都由你在这台 iPhone 上确认。', 'An Agent asks; you confirm each one on this iPhone.') },
  ],
  recent: [
    { at: t('今天 08:12', 'Today 08:12'), text: t('Agent 读取了近 7 天的步数和睡眠 ·「晨间简报」', 'An Agent read 7 days of steps and sleep · “Morning brief”') },
    { at: t('昨天 22:40', 'Yesterday 22:40'), text: t('创建闹钟「06:30 起床」，已在 iPhone 上确认', 'Alarm “06:30 Wake up” created, confirmed on the iPhone') },
  ],
  ...extra,
});

const macMini: Device = {
  id: 'mini', kind: 'desktop', name: 'Mac mini（办公室）', app: 'Avibe Desktop 3.2.8 · macOS',
  online: { path: 'tunnel', ms: 120 }, signedIn: t('今天 09:41', 'Today 09:41'), capabilities: [],
};
const chrome: Device = {
  id: 'chrome', kind: 'browser', name: 'Chrome · Windows', app: '浏览器',
  lastActive: t('3 天前', '3 days ago'), signedIn: t('11 月 17 日', 'Nov 17'), capabilities: [],
};
const safari: Device = {
  id: 'safari', kind: 'browser', name: 'Safari · iPad', app: '主屏幕 App',
  lastActive: t('昨天 21:15', 'Yesterday 21:15'), signedIn: t('10 月 30 日', 'Oct 30'),
  capabilities: [{ kind: 'notifications', on: true, detail: t('伙伴和会话的消息会推送到这台 iPad。', 'Messages from Bots and Sessions are pushed to this iPad.') }],
};
const sara: Device = {
  id: 'sara', kind: 'browser', name: 'Chrome · macOS', app: '浏览器', person: 'sara@example.test',
  online: { path: 'tunnel' }, signedIn: t('今天 10:02', 'Today 10:02'), capabilities: [],
};

const activity: Activity[] = [
  { at: t('今天 09:41', 'Today 09:41'), text: t('Mac mini（办公室）登录', 'Mac mini (office) signed in'), device: 'Avibe Desktop' },
  { at: t('今天 08:12', 'Today 08:12'), text: t('Agent 读取了近 7 天的步数和睡眠 ·「晨间简报」', 'An Agent read 7 days of steps and sleep · “Morning brief”'), device: 'Max 的 iPhone' },
  { at: t('昨天 22:40', 'Yesterday 22:40'), text: t('创建闹钟「06:30 起床」，已在 iPhone 上确认', 'Alarm “06:30 Wake up” created, confirmed on the iPhone'), device: 'Max 的 iPhone' },
  { at: t('昨天 19:03', 'Yesterday 19:03'), text: t('已退出 Firefox · Linux', 'Firefox · Linux signed out'), device: 'Max 的 MacBook Pro' },
];
const fullActivity: Activity[] = [
  ...activity,
  { at: t('11 月 17 日 14:20', 'Nov 17 14:20'), text: t('Chrome · Windows 登录', 'Chrome · Windows signed in'), device: '浏览器' },
  { at: t('11 月 15 日 20:31', 'Nov 15 20:31'), text: t('直连已开启', 'Direct turned on'), device: 'Max 的 MacBook Pro' },
  { at: t('11 月 12 日 09:06', 'Nov 12 09:06'), text: t('Max 的 iPhone 已连接健康：步数、活动、锻炼、睡眠', 'Max’s iPhone connected Health: steps, activity, workouts, sleep'), device: 'Max 的 iPhone' },
  { at: t('11 月 12 日 09:02', 'Nov 12 09:02'), text: t('Max 的 iPhone 登录', 'Max’s iPhone signed in'), device: 'Avibe App' },
  { at: t('10 月 30 日 18:44', 'Oct 30 18:44'), text: t('Safari · iPad 登录', 'Safari · iPad signed in'), device: '主屏幕 App' },
  { at: t('10 月 30 日 18:40', 'Oct 30 18:40'), text: t('远程访问已开启：max-app.avibe.bot', 'Remote access turned on: max-app.avibe.bot'), device: 'Max 的 MacBook Pro' },
];

const on: Reach = { state: 'on', host, account, tunnel: 'good', ms: 120, direct: true };
const all = (self: string): Device[] => [macbook(), iphone(), macMini, safari, chrome].map((each) => (each.id === self ? { ...each, self: true } : each));

const at = (height: number) => ({ width: 1440, height });
const TALL = at(1000);

function frame(
  name: string,
  title: string,
  size: { width: number; height: number; safeArea?: { top: number; bottom: number } },
  props: DevicesPageProps,
  extra: Partial<FrameSpec> & { app?: boolean } = {},
): FrameSpec {
  const { app, ...spec } = extra;
  return {
    name,
    title,
    ...size,
    status: 'proposed',
    themes: ['light'],
    route,
    fixtures: fixtures(app ? { nativeShell: IOS_APP_CAPABILITIES } : {}),
    render: settingsPage('remote-access', <DevicesPage ownerEmail={account} {...props} />),
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
  frame('off-desktop-name-taken', '未开启 · Desktop App：名称已被使用', at(760),
    { reach: { state: 'off', surface: 'desktop', account, name: 'max', nameState: 'taken' }, devices: [macbook({ self: true })] }),
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
    { reach: on, viewer: 'member', devices: [{ ...sara, person: undefined, self: true }, { ...iphone({ id: 'sara-phone', name: 'Sara 的 iPhone', online: undefined, lastActive: t('昨天 18:20', 'Yesterday 18:20'), capabilities: [iphone().capabilities[0]] }) }] }),

  // 4. A device's detail.
  frame('detail-iphone', '设备详情 · iPhone：连接、三项功能各一个开关、最近、退出这台设备', TALL,
    { reach: on, devices: all('host'), activity, detail: 'iphone' }),
  frame('detail-iphone-phone', '设备详情 · 手机宽度（底部面板）', { ...IPHONE_17_PRO, height: 900 },
    { reach: on, devices: all('safari'), activity, detail: 'iphone' }),
  frame('detail-self-app', '设备详情 · 在 iPhone App 里看自己：多一行「这台设备走 自动 | 仅 Cloudflare」（11-19 连接路径卡的本设备选择）', { ...IPHONE_17_PRO, height: 960 },
    { reach: on, activity, detail: 'iphone', devices: all('iphone').map((each) => (each.id === 'iphone' ? { ...each, pathChoice: 'auto' as const } : each)) }, { app: true }),
  frame('detail-confirm', '退出 iPhone：会删除健康数据，所以先确认', at(860),
    { reach: on, devices: all('host'), activity, detail: 'iphone', confirm: 'remove' }),
  frame('detail-browser', '设备详情 · 浏览器：没有会丢的东西，退出不需要确认', at(860),
    { reach: on, devices: all('host'), activity, detail: 'chrome' }),
  frame('detail-host', '设备详情 · 运行 Avibe 的电脑：没有「退出」', at(860),
    { reach: on, devices: all('host'), activity, detail: 'host' }),
  frame('detail-host-remote', '设备详情 · 从别的设备看主机：电脑操作只能在那台电脑上改', at(860),
    { reach: on, activity, detail: 'host', devices: all('mini').map((each) => (each.id === 'host' ? macbook({ capabilities: [
      { kind: 'computerUse', on: true, detail: t('', ''), fixed: t('已就绪。在那台 Mac 的 Avibe 里开关。', 'Ready. Turn it on or off in Avibe on that Mac.') },
      macbook().capabilities[1],
    ] }) : each)) }),
  frame('detail-other-person', '设备详情 · 别人的设备：只结束这次访问', at(1080),
    { reach: on, devices: [...all('host'), sara], activity, detail: 'sara' }),

  // 5. Activity.
  frame('activity', '活动：登录、退出和功能使用，按时间', at(860),
    { reach: on, devices: all('host'), activity: fullActivity, view: 'activity' }),

  // 6. Empty and error states that really occur.
  frame('empty', '刚开启，还没有别的设备', at(680),
    { reach: on, devices: [macbook({ self: true })] }),
  frame('tunnel-down', '已开启，但连不上 Cloudflare', at(760),
    { reach: { ...on, tunnel: 'down' } as Reach, devices: [macbook({ self: true }), iphone({ online: undefined, lastActive: t('12 分钟前', '12 min ago') }), { ...macMini, online: undefined, lastActive: t('12 分钟前', '12 min ago') }] }),
  frame('paused', '关闭后：域名保留，一个开关就能再开', at(700),
    { reach: { state: 'paused', host, account }, devices: [macbook({ self: true }), iphone({ online: undefined, lastActive: t('2 小时前', '2 h ago') })] }),
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
