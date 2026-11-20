// Proposed 2026-11-20 (frames only, a draft for the owner to discuss; no implementation): the
// 「连接方式」 page behind the Devices page's row of that name (kit/devices-connection.tsx). Every
// path with its state, whose choice each setting is (this Avibe, or this device), today's Cloudflare
// tunnel settings moved here unchanged, and the trial that puts a remote change back when the
// device that made it does not come back. What exists today and what is new work: the Show Page.
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO, type WorkbenchFixtures } from '../../shell/fixtures';
import { frames, type FrameSpec } from '../../frame';
import { ConnectionPage, type ConnectionPageProps } from '../../kit/devices-connection';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';
const copy = { 'settings.sections.remoteAccess': { zh: '设备', en: 'Devices' } };
const fixtures = (extra: WorkbenchFixtures = {}) => settingsFixtures({}, { copy, ...extra });
const t = (zh: string, en: string) => ({ zh, en });

const tunnel: ConnectionPageProps['tunnel'] = { ms: 120, protocol: 'auto', active: 'QUIC', ip: '4', autoRecovery: true, profile: 'balanced' };
const base: ConnectionPageProps = { viewer: 'browser', tunnel, direct: 'ready', relay: true };

function frame(
  name: string,
  title: string,
  size: { width: number; height: number; safeArea?: { top: number; bottom: number } },
  props: ConnectionPageProps,
  themes: FrameSpec['themes'] = ['light'],
): FrameSpec {
  return {
    name, title, ...size, status: 'proposed', themes, route,
    fixtures: fixtures(props.viewer === 'app' ? { nativeShell: IOS_APP_CAPABILITIES } : {}),
    render: settingsPage('remote-access', <ConnectionPage {...props} />),
  };
}

const WIDE = { width: 1440, height: 1200 };

export default frames([
  frame('browser', '连接方式 · 从别的电脑的浏览器看：这个 Avibe 提供的路径、隧道设置', WIDE, base, ['light', 'dark']),
  frame('app', '连接方式 · 在 iPhone App 里：多一组「这台设备」，正在走打洞直连', { ...IPHONE_17_PRO, height: 1900 },
    { ...base, viewer: 'app', path: { kind: 'punched', family: 'IPv6', ms: 38 } }),
  frame('direct-denied', '连接方式 · 直连未获准：每条受影响的路径写明原因', WIDE,
    { ...base, direct: 'denied' }),
  frame('direct-off', '连接方式 · 在本机看、直连未开启：设置立即生效，没有试用', WIDE,
    { ...base, viewer: 'local', direct: 'off', relay: false }),
  frame('trial', '连接方式 · 远程把传输协议固定为 HTTP/2：试用，不保留就自动改回', WIDE,
    { ...base, tunnel: { ...tunnel, protocol: 'http2', active: 'HTTP/2' }, trial: { what: t('传输协议已改为 HTTP/2', 'The transport protocol is now HTTP/2'), back: t('「自动」', 'Automatic'), seconds: 24 } }),
  frame('reverted', '连接方式 · 没有连回来：已自动改回，回来时说明', WIDE,
    { ...base, reverted: { what: t('传输协议固定为 HTTP/2', 'fixing the transport protocol to HTTP/2'), back: t('「自动」', 'Automatic') } }),
  // Changing the domain: a plain rename. The old prefix is void at once and is freed for others
  // after a waiting period (one number; 30 here, the owner has not fixed it yet).
  frame('domain', '连接方式 · 页面最下面一行：域名，和「更改域名」', WIDE, { ...base, viewer: 'local', host: 'max-app.avibe.bot' }),
  frame('rename', '更改域名 · 第一步：新名称，和会发生什么', WIDE,
    { ...base, viewer: 'local', host: 'max-app.avibe.bot', rename: { step: 'form', name: 'maxwell', holdDays: 30 } }),
  frame('rename-taken', '更改域名 · 名称已被占用', WIDE,
    { ...base, viewer: 'local', host: 'max-app.avibe.bot', rename: { step: 'form', name: 'alex', taken: true, holdDays: 30 } }),
  frame('rename-confirm', '更改域名 · 第二次确认：按钮先停 3 秒', WIDE,
    { ...base, viewer: 'local', host: 'max-app.avibe.bot', rename: { step: 'confirm', name: 'maxwell', holdDays: 30 } }),
  frame('renamed', '更改域名 · 之后（在这台电脑上看）', WIDE,
    { ...base, viewer: 'local', host: 'maxwell-app.avibe.bot', rename: { step: 'done', name: 'maxwell', from: 'max-app.avibe.bot', holdDays: 30 } }),
]);
