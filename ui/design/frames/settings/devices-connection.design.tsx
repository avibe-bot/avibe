// The 「连接方式」 page behind the Devices page's row of that name
// (src/components/devices/ConnectionPageView.tsx). Every path with its state, whose choice each
// setting is (this Avibe, or this device), the Cloudflare tunnel settings drawn with Remote access's
// own controls, the trial that puts a remote change back when the device that made it does not come
// back, and the domain with its plain rename. Not routed yet; drawn at the route it will take over.
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO, type WorkbenchFixtures } from '../../shell/fixtures';
import { DESIGN_NOW } from '../../shell/clock';
import { frames, type FrameSpec } from '../../frame';
import { ConnectionPageView, type ConnectionPageViewProps } from '@/components/devices/ConnectionPageView';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';
const copy = { 'settings.sections.remoteAccess': { zh: '设备', en: 'Devices' } };
const fixtures = (extra: WorkbenchFixtures = {}) => settingsFixtures({}, { copy, ...extra });

const tunnel: ConnectionPageViewProps['tunnel'] = {
  ms: 120,
  active: 'quic',
  settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: '4', edge_bind_address: '' },
  interfaces: [{ id: 'en0', name: 'Wi-Fi', address: '192.168.1.23', ip_version: '4' }],
};
// The relay's switch is shown: whether it stays a switch is pending with Product, and the container decides.
const base: ConnectionPageViewProps = { viewer: 'browser', tunnel, direct: 'ready', relay: true, relayEditable: true };
const host = 'max-app.avibe.bot';
/** How long a voided name is held before someone else may claim it (owner, 2026-11-20). */
const voidDays = 7;

function frame(
  name: string,
  title: string,
  size: { width: number; height: number; safeArea?: { top: number; bottom: number } },
  props: ConnectionPageViewProps,
  themes: FrameSpec['themes'] = ['light'],
): FrameSpec {
  return {
    name, title, ...size, status: 'shipped', themes, route,
    fixtures: fixtures(props.viewer === 'app' ? { nativeShell: IOS_APP_CAPABILITIES } : {}),
    render: settingsPage('remote-access', <ConnectionPageView {...props} />),
  };
}

const WIDE = { width: 1440, height: 1200 };
const APP = { ...IPHONE_17_PRO, height: 1900 };
const app: ConnectionPageViewProps = {
  ...base,
  viewer: 'app',
  path: { kind: 'punched', family: 'ipv6', rttMs: 38 },
  preference: { mode: 'automatic', reverted: null },
};

const designFrames = frames([
  frame('browser', '连接方式 · 从别的电脑的浏览器看：这个 Avibe 提供的路径、隧道设置', WIDE, base, ['light', 'dark']),
  frame('browser-phone', '连接方式 · 375 宽的浏览器', { width: 375, height: 2000 }, base),
  frame('app', '连接方式 · 在 iPhone App 里：多一组「这台设备」（自动 / 仅 Cloudflare 隧道），正在走打洞直连', APP, app),
  frame('app-reverted', '连接方式 · App 选了「仅 Cloudflare 隧道」但连不上，已自己改回「自动」：一行说明', APP,
    { ...app, path: { kind: 'tunnel', relayUp: false }, preference: { mode: 'automatic', reverted: { at: new Date(DESIGN_NOW - 25 * 60_000).toISOString(), reason: 'unreachable' } } }),
  frame('direct-denied', '连接方式 · 直连未获准：每条受影响的路径写明原因', WIDE,
    { ...base, direct: 'denied' }),
  frame('direct-off', '连接方式 · 在本机看、直连未开启：设置立即生效，没有试用', WIDE,
    { ...base, viewer: 'local', direct: 'off', relay: false }),
  frame('trial', '连接方式 · 远程把传输协议固定为 HTTP/2：试用，不保留就自动改回', WIDE,
    { ...base, tunnel: { ...tunnel, active: 'http2', settings: { ...tunnel.settings, transport_protocol: 'http2' } }, trial: { change: { setting: 'protocol', to: 'http2', from: 'auto' }, seconds: 24 } }),
  frame('reverted', '连接方式 · 没有连回来：已自动改回，回来时说明', WIDE,
    { ...base, reverted: { setting: 'protocol', to: 'http2', from: 'auto' } }),
  // Changing the domain: a plain rename. The old prefix is void at once and is freed for others
  // after a waiting period, passed in (7 days, the same as deleting an instance).
  frame('domain', '连接方式 · 页面最下面一行：域名，和「更改域名」', WIDE, { ...base, viewer: 'local', host }),
  frame('rename', '更改域名 · 第一步：新名称，和会发生什么', WIDE,
    { ...base, viewer: 'local', host, rename: { step: 'form', name: 'maxwell', voidDays, health: true } }),
  frame('rename-taken', '更改域名 · 名称已被占用', WIDE,
    { ...base, viewer: 'local', host, rename: { step: 'form', name: 'alex', nameState: 'taken', voidDays, health: true } }),
  frame('rename-confirm', '更改域名 · 第二次确认：按钮先停 3 秒', WIDE,
    { ...base, viewer: 'local', host, rename: { step: 'confirm', name: 'maxwell', countdown: 3 } }),
  frame('renamed', '更改域名 · 之后（在这台电脑上看）', WIDE,
    { ...base, viewer: 'local', host: 'maxwell-app.avibe.bot', rename: { step: 'done', from: host } }),
]);

export default designFrames;
