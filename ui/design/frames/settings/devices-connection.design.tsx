// Settings · Devices · 连接方式 (src/components/devices/ConnectionPageView.tsx), the page behind
// the Devices page's 「连接方式」 row, at `/settings/remote-access/connection`. The instance verifies
// a changed tunnel setting itself: one status line, no keep button, no countdown. Direct and the
// relay are ordinary switches, refused with one sentence. The relay row exists only on an instance
// enrolled in the relay trial. Contract: docs/plans/devices-connection-settings.md. Every frame
// draws the shipped view with the data `ConnectionPage` would feed it.
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { DESIGN_NOW } from '../../shell/clock';
import { frames, type FrameSpec } from '../../frame';
import { ConnectionPageView, type ConnectionPageViewProps } from '@/components/devices/ConnectionPageView';
import type { AddressState, ConnectionState, RebindState, TunnelChange } from '@/components/devices/types';
import type { RemoteAccessStatus } from '@/context/ApiContext';
import { settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access/connection';
const host = 'max-app.avibe.bot';
const ago = (minutes: number) => new Date(DESIGN_NOW - minutes * 60_000).toISOString();

const connection = (extra: Partial<ConnectionState> = {}): ConnectionState => ({
  settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' },
  interfaces: [
    { id: 'en0', name: 'Wi-Fi', address: '192.168.1.24', ip_version: '4' },
    { id: 'en7', name: 'Ethernet', address: '10.0.0.8', ip_version: '4' },
  ],
  direct: 'ready',
  ...extra,
});
const pinned = (protocol: 'quic' | 'http2', extra: Partial<ConnectionState> = {}) => connection({
  settings: { ...connection().settings, transport_protocol: protocol },
  ...extra,
});
const address: AddressState = {
  hostname: host, eligible: true, holdDays: 30, releaseAtIfChangedNow: '2026-12-23T00:00:00.000Z', retired: [], healthConnected: true,
};
const ready: RebindState = { state: 'ready' };

const base: ConnectionPageViewProps = {
  load: 'ready', connection: connection(), tunnel: { state: 'up', ms: 120, active: 'quic' }, remote: false,
  host, address, rebind: ready,
};
const remote: Partial<ConnectionPageViewProps> = { remote: true, via: 'tunnel', rebind: { state: 'remote' } };
const changed = (change: TunnelChange, protocol: 'quic' | 'http2' | 'auto' = 'http2'): Partial<ConnectionPageViewProps> => ({
  connection: protocol === 'auto' ? connection({ change }) : pinned(protocol, { change }),
});

const status: RemoteAccessStatus = {
  ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}`, transport_protocol: 'auto',
  network_path: {
    provider: 'Cloudflare', asn: 13335, client_access: 'local', client_ingress: null,
    connector: {
      locations: [{ id: 'sjc05', colo: 'SJC', location: 'San Jose' }, { id: 'sjc07', colo: 'SJC', location: 'San Jose' }, { id: 'lax01', colo: 'LAX', location: 'Los Angeles' }],
      edge_ips: ['198.41.200.13', '198.41.192.77'],
    },
    route: { assessment: 'same_country' },
  },
} as unknown as RemoteAccessStatus;

const WIDE = { width: 1440, height: 1300 };
const PHONE = { width: 375, height: 2000 };

function frame(name: string, title: string, size: { width: number; height: number; safeArea?: { top: number; bottom: number } }, props: Partial<ConnectionPageViewProps>, extra: Partial<FrameSpec> & { app?: boolean } = {}): FrameSpec {
  const { app, ...spec } = extra;
  return {
    name, title, ...size, status: 'shipped', themes: ['light'], route,
    fixtures: settingsFixtures({}, app ? { nativeShell: IOS_APP_CAPABILITIES } : {}),
    render: settingsPage('remote-access/connection', <ConnectionPageView {...base} {...props} />),
    ...spec,
  };
}

const designFrames = frames([
  frame('local', '连接方式 · 在运行 Avibe 的电脑上：全部自动；最下面是域名、更改域名、重新绑定', WIDE, {}, { themes: ['light', 'dark'] }),
  frame('local-phone', '连接方式 · 375 宽', PHONE, {}),
  // A build before renaming and binding again are live: the instance says the name cannot be
  // changed here and has no bind-again overview, so the last panel is the domain and nothing else.
  frame('local-domain-only', '连接方式 · 改名和重新绑定还没有上线的版本：最下面只有域名一行，没有按钮、没有说明', WIDE,
    { address: { ...address, eligible: false, ineligibleReason: 'address_state_unknown' }, rebind: undefined }),
  frame('remote-domain-only', '连接方式 · 同一个版本从远处看：也只有域名一行，不提重新绑定', WIDE,
    { ...remote, address: { ...address, eligible: false, ineligibleReason: 'address_state_unknown' }, rebind: undefined }),
  frame('remote-browser', '连接方式 · 浏览器远程打开：走的是 Cloudflare 隧道；重新绑定要回到那台电脑上做', WIDE, remote),
  frame('app', '连接方式 · Avibe App：「这台设备」只有 自动 / 仅 Cloudflare 隧道，自动在前', { ...IPHONE_17_PRO, height: 2300 },
    { ...remote, via: 'direct', path: { kind: 'punched', family: 'ipv6', rttMs: 38 }, preference: { mode: 'automatic', reverted: null } }, { app: true }),
  frame('app-tunnel-only', '连接方式 · Avibe App 选了「仅 Cloudflare 隧道」', { ...IPHONE_17_PRO, height: 2300 },
    { ...remote, path: { kind: 'tunnel', relayUp: false }, preference: { mode: 'tunnel', reverted: null } }, { app: true }),
  frame('app-reverted', '连接方式 · App 连不上后自己改回了「自动」：只带时间，不带秒数', { ...IPHONE_17_PRO, height: 2300 },
    { ...remote, path: null, preference: { mode: 'automatic', reverted: { at: ago(95), reason: 'unreachable' } } }, { app: true }),
  frame('loading', '连接方式 · 第一次读', { width: 1440, height: 700 }, { load: 'loading', connection: null }),
  frame('unavailable', '连接方式 · 现在读不准状态：不显示这些设置，可以重试', { width: 1440, height: 700 }, { load: 'unavailable', connection: null }),

  // The tunnel's own settings: one status line per change.
  frame('change-applying', '改了传输协议 · 正在应用：控件等着', WIDE, changed({ setting: 'protocol', state: 'applying' })),
  frame('change-verifying', '改了传输协议 · 验证中', WIDE, changed({ setting: 'protocol', state: 'verifying' })),
  frame('change-applied', '改了传输协议 · 已生效', WIDE, changed({ setting: 'protocol', state: 'applied' })),
  frame('change-saved', '隧道没在运行时改了传输协议 · 只是存下了，说「已保存」，不说已生效', WIDE,
    { ...changed({ setting: 'protocol', state: 'applied', saved: true }), tunnel: { state: 'down', active: null } }),
  frame('change-restoring', '改了传输协议 · 没连上，正在改回', WIDE, changed({ setting: 'protocol', state: 'restoring', from: 'auto' })),
  frame('change-restored', '改了传输协议 · 没连上，已改回', WIDE, changed({ setting: 'protocol', state: 'restored', reason: 'verification_failed', from: 'auto' }, 'auto')),
  frame('change-restored-unverifiable', '改了出口网卡 · 无法验证，已改回', WIDE, changed({ setting: 'outbound', state: 'restored', reason: 'verification_unavailable', from: '' }, 'auto')),
  frame('change-superseded', '改了边缘网络 IP · 被后来的更改取代', WIDE, changed({ setting: 'ipFamily', state: 'superseded' }, 'auto')),
  frame('change-runtime-pending', '改了传输协议 · 已改回，隧道还在恢复', WIDE, changed({ setting: 'protocol', state: 'failed', reason: 'saved_restored_runtime_pending', from: 'auto' }, 'auto')),
  frame('change-storage-failed', '改了传输协议 · 没能改回原来的设置', WIDE, changed({ setting: 'protocol', state: 'failed', reason: 'storage_restore_failed', from: 'auto' })),
  frame('change-not-ready', '改了传输协议 · 新的连接没有建立起来', WIDE, changed({ setting: 'protocol', state: 'failed', reason: 'candidate_not_ready', from: 'auto' }, 'auto')),
  frame('change-busy', '改了边缘网络 IP · 另一项更改还在进行', WIDE, changed({ setting: 'ipFamily', state: 'failed', reason: 'connection_change_busy', from: 'auto' }, 'auto')),
  frame('change-unknown', '改了传输协议 · 应答丢了：不说失败，重新连上后读结果', WIDE, changed({ setting: 'protocol', state: 'unknown' })),
  frame('change-unknown-phone', '应答丢了 · 375 宽', PHONE, changed({ setting: 'protocol', state: 'unknown' })),
  frame('returned-to-automatic', '固定的设置连续 3 分钟连不上：只列真正改回自动的那几项', WIDE,
    { connection: connection({ returnedToAutomatic: { at: ago(42), changes: [{ setting: 'protocol', from: 'http2' }, { setting: 'outbound', from: '10.0.0.8' }] } }) }),
  frame('returned-to-automatic-phone', '改回自动的记录 · 375 宽', PHONE,
    { connection: connection({ returnedToAutomatic: { at: ago(42), changes: [{ setting: 'protocol', from: 'http2' }] } }) }),
  frame('temporary-route', '隧道临时走另一组设置，保存的设置没变：一行如实说', WIDE,
    { connection: pinned('quic', { temporaryRoute: { protocol: 'http2', differences: ['protocol'] } }), tunnel: { state: 'up', ms: 180, active: 'http2' } }),
  frame('temporary-route-phone', '临时线路 · 375 宽', PHONE,
    { connection: pinned('quic', { temporaryRoute: { protocol: 'http2', differences: ['protocol', 'ipFamily'] } }) }),

  // What this Avibe offers.
  frame('tunnel-down', '隧道没连上（没在运行，或一条连接都没有）', WIDE, { tunnel: { state: 'down' } }),
  frame('tunnel-slow', '隧道连着但很慢：仍然可用，下面一行说慢', WIDE, { tunnel: { state: 'slow', ms: 420, active: 'quic' } }),
  frame('tunnel-unsteady', '隧道连着但不太稳定', WIDE, { ...remote, tunnel: { state: 'unsteady', ms: 150, active: 'quic' } }),
  frame('tunnel-unknown', '隧道的状态读不到', WIDE, { tunnel: { state: 'unknown' } }),
  frame('setting-refused', '改「优化策略」被拒：另一项更改还在进行，下面说一句', WIDE, { refused: { row: 'profile', reason: 'connection_change_busy' } }),
  frame('direct-off', '直连关着：局域网和打洞两行不出现', WIDE, { connection: connection({ direct: 'off' }) }),
  frame('direct-awaiting-permission', '直连 · 正在申请许可', WIDE, { connection: connection({ direct: 'awaiting-permission' }) }),
  frame('direct-denied', '直连 · 还没有对这个 Avibe 开放', WIDE, { connection: connection({ direct: 'denied' }) }),
  frame('direct-awaiting-certificate', '直连 · 等证书', WIDE, { connection: connection({ direct: 'awaiting-certificate' }) }),
  frame('direct-not-running', '直连 · 组件没有运行', WIDE, { connection: connection({ direct: 'not-running' }) }),
  frame('direct-refused', '远程关直连被拒：确认不了隧道是通的，一句话', WIDE, { ...remote, refused: { row: 'direct', reason: 'tunnel_unhealthy' } }),
  frame('direct-refused-busy', '关直连被拒：另一项更改还在进行', WIDE, { refused: { row: 'direct', reason: 'connection_change_busy' } }),
  frame('relay', '加入了中继试用的实例：多一行中继，开着并已连上', WIDE, { connection: connection({ relay: { enabled: true, connected: true } }) }),
  frame('relay-off', '中继 · 关着', WIDE, { connection: connection({ relay: { enabled: false, connected: false } }) }),
  frame('relay-no-permission', '中继 · 暂时开不了：还没有直连的许可', WIDE,
    { connection: connection({ direct: 'denied', relay: { enabled: false, connected: false, unavailable: 'direct_permission_required' } }) }),
  frame('relay-refused', '远程改中继被拒：确认不了隧道是通的', WIDE,
    { ...remote, connection: connection({ relay: { enabled: true, connected: false } }), refused: { row: 'relay', reason: 'tunnel_unhealthy' } }),
  frame('relay-phone', '中继 · 375 宽', PHONE, { connection: connection({ relay: { enabled: true, connected: false } }) }),

  // 优化线路, 连接诊断与网络路径.
  frame('optimize-started', '优化线路 · 已开始', WIDE, { optimize: { started: true } }),
  frame('diagnostics', '连接诊断与网络路径 · 诊断跑完；直连关着，没有直连那两行', WIDE,
    { diagnostics: { status, now: DESIGN_NOW, diagnostics: { ok: true, sampled_at: ago(0), effective_protocol: 'quic', dns: { status: 'available' }, quic: { status: 'available', source: 'probe' }, http2: { status: 'unknown', source: 'probe' }, cloudflared_version: '2026.10.1' } } }),
  frame('diagnostics-direct', '连接诊断与网络路径 · 直连开着：多两行，证书到期日和最近一次连接两条路各用了多久（App 里才有记录）', WIDE,
    { ...remote, via: 'direct', diagnostics: { status, now: DESIGN_NOW, diagnostics: null, direct: { certificate: new Date('2027-02-14T00:00:00Z'), lastConnection: { direct: 38, public: 212 } } } }),
  frame('diagnostics-direct-browser', '连接诊断与网络路径 · 直连开着，在浏览器里看：证书照常，最近一次连接没有记录', WIDE,
    { diagnostics: { status, now: DESIGN_NOW, diagnostics: null, direct: { certificate: new Date('2027-02-14T00:00:00Z'), lastConnection: null } } }),
  frame('diagnostics-direct-phone', '直连的两行 · 375 宽', { width: 375, height: 900 },
    { ...remote, via: 'direct', diagnostics: { status, now: DESIGN_NOW, diagnostics: null, direct: { certificate: null, lastConnection: { direct: 'timeout', public: 180 } } } }),
  frame('diagnostics-phone', '连接诊断与网络路径 · 375 宽', { width: 375, height: 900 }, { diagnostics: { status, now: DESIGN_NOW, diagnostics: null } }),
]);

export default designFrames;
