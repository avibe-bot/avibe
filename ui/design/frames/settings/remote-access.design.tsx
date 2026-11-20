// Settings · Remote access: pair this machine with Avibe Cloud and reach it from anywhere.
import { RemoteAccessPage } from '@/components/RemoteAccessPage';
import type { RemoteAccessStatus, TunnelQualitySnapshot } from '@/context/ApiContext';
import { DESIGN_NOW } from '../../shell/clock';

import { frames } from '../../frame';
import { IOS_APP_CAPABILITIES } from '../../shell/fixtures';
import { settingsConfig, settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('remote-access', <RemoteAccessPage />);
const route = '/settings/remote-access';

const unpaired = settingsFixtures({ '/api/remote-access/status': { ok: true, enabled: false, paired: false, running: false } });

const pairedConfig = {
  ...settingsConfig,
  remote_access: {
    ...settingsConfig.remote_access,
    vibe_cloud: { ...settingsConfig.remote_access.vibe_cloud, enabled: true, paired: true, public_url: 'https://demo-mac.app.avibe.bot' },
  },
};
const paired = settingsFixtures({
  '/api/config': pairedConfig,
  '/api/remote-access/status': {
    ok: true,
    enabled: true,
    paired: true,
    running: true,
    public_url: 'https://demo-mac.app.avibe.bot',
    pid_state: 'running',
    transport_protocol: 'auto',
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: '4', edge_bind_address: '' },
  },
  '/api/remote-access/network-interfaces': { ok: true, interfaces: [{ id: 'en0', name: 'Wi-Fi', address: '192.168.1.23', ip_version: '4' }] },
});

const waitingQuality: TunnelQualitySnapshot = {
  schema_version: 2, state: 'degraded', grade: 'poor', sampled_at: new Date(DESIGN_NOW).toISOString(),
  protocol: 'quic', connector_count: 1, ha_connections: 4, rtt_ms: { min: 200, median: 250, max: 350 },
  baseline_median_rtt_ms: 100, edge_locations: ['iad01'], window_seconds: 60,
  request_errors_per_minute: 0, packet_loss_per_minute: 0,
  recovery: { state: 'cooldown', last_attempt_at: new Date(DESIGN_NOW - 60_000).toISOString(),
    last_trigger: 'latency', last_result: 'no_improvement', previous_median_rtt_ms: 250,
    result_median_rtt_ms: 240, next_attempt_at: new Date(DESIGN_NOW + 120_000).toISOString(), attempt_count_window: 2 },
};
const automaticWait = settingsFixtures({
  ...paired.api,
  '/api/remote-access/status': {
    ...(paired.api?.['/api/remote-access/status'] as RemoteAccessStatus), tunnel_quality: waitingQuality,
  },
});

// Approved by Product 2026-11-18 and shipped with #3940: direct, as current facts only. Shown while direct is turned on; the page line
// only once this page's live stream has answered for itself.
const directOn = (state: string, reason: string, pageVia?: 'direct' | 'other') => settingsFixtures({
  ...paired.api,
  '/api/remote-access/status': {
    ...(paired.api?.['/api/remote-access/status'] as RemoteAccessStatus),
    direct: {
      enabled: true, state, reason, certificate_not_after: '2027-02-14T08:00:00Z',
      transport_running: state === 'running', direct_access_allowed: state === 'running',
    },
  },
}, pageVia ? { stream: [{ after: 0, event: 'connected', raw: true, data: { sub_id: 1, via: pageVia } }] } : {});
const directFrame = (name: string, title: string, fixtures: ReturnType<typeof directOn>) => (
  { name, title, width: 390, height: 1000, status: 'shipped' as const, route, fixtures, render: page, wait: 300 }
);

// In the App (`direct` version 3): under the direct row, what this device is using, in the four agreed
// names (Product, 2026-11-19): the Cloudflare tunnel, the relay, or a hole-punched or LAN direct path
// with its family and measured round trip. Whether the page is on the direct ingress comes from its own
// stream; which kind of path from the App. A relayed path is never called direct. Then the last new
// connection's race. While the page is on the direct ingress, Cloudflare's quality and network path are
// the fallback route: labelled so, and the path folded.
const cloudflarePath = {
  schema_version: 1, provider: 'Cloudflare', asn: 13335, sampled_at: new Date(DESIGN_NOW).toISOString(),
  locations_pending: false, client_access: 'remote', client_ingress: { colo: 'SIN', location: 'Singapore' },
  connector: { locations: [{ id: 'sin09', colo: 'SIN', location: 'Singapore' }], edge_ips: ['198.41.192.47'] },
  route: { assessment: 'same_metro' },
};
const directInApp = (
  pageVia: 'direct' | 'other',
  app: { preferred?: 'direct' | 'tunnel'; path?: 'relay' | 'ipv4' | 'ipv6'; path_scope?: 'local' | 'global'; rtt_ms?: number },
  connection: { direct: number | string; public: number | string; winner: string },
) => {
  const base = directOn('running', 'published', pageVia);
  const status = base.api?.['/api/remote-access/status'] as RemoteAccessStatus;
  return {
    ...base,
    api: { ...base.api, '/api/remote-access/status': { ...status, network_path: cloudflarePath } },
    nativeShell: { ...IOS_APP_CAPABILITIES, direct: 3 },
    nativeDirectStatus: { preferred: 'direct', sequence: 3, closure: 0, ...app, connections: [connection] },
  };
};

const designFrames = frames([
  { name: 'unpaired', title: '设置 · 远程访问 · 未配对', width: 1440, height: 900, status: 'shipped', route, fixtures: unpaired, render: page },
  { name: 'running', title: '设置 · 远程访问 · 已配对、运行中', width: 1440, height: 900, status: 'shipped', route, fixtures: paired, render: page },
  { name: 'automatic-wait', title: 'Remote access · automatic retry, manual available', width: 1440, height: 900, status: 'shipped', route, fixtures: automaticWait, render: page },
  { name: 'automatic-wait-phone', title: 'Remote access · automatic retry on mobile', width: 390, height: 1100, status: 'shipped', route, fixtures: automaticWait, render: page },
  { name: 'phone', title: '设置 · 远程访问 · 手机', width: 390, height: 844, status: 'shipped', route, fixtures: unpaired, render: page },
  directFrame('direct-page-direct', '直连 · 已就绪（证书未过期才显示日期），本页经直连', directOn('running', 'published', 'direct')),
  directFrame('direct-page-other', '直连 · 已就绪，本页未走直连', directOn('running', 'published', 'other')),
  directFrame('direct-app-punched', 'App · 本设备正在使用：打洞直连 · IPv6 · 85 ms；Cloudflare 是备用路径，已折叠',
    directInApp('direct', { path: 'ipv6', path_scope: 'global', rtt_ms: 85 }, { direct: 42, public: 'none', winner: 'direct' })),
  directFrame('direct-app-lan', 'App · 本设备正在使用：局域网直连 · IPv4 · 3 ms',
    directInApp('direct', { path: 'ipv4', path_scope: 'local', rtt_ms: 3 }, { direct: 8, public: 'none', winner: 'direct' })),
  directFrame('direct-app-unclassed', 'App · 分不清局域网和打洞时（较早的 App）：直连 · IPv4，不显示往返时间',
    directInApp('direct', { path: 'ipv4' }, { direct: 40, public: 'none', winner: 'direct' })),
  directFrame('direct-app-relay-in-use', 'App · 本设备正在使用：中继 · 140 ms（从不称为直连）；Cloudflare 是备用路径',
    directInApp('direct', { path: 'relay', rtt_ms: 140 }, { direct: 310, public: 'cancelled', winner: 'direct' })),
  directFrame('direct-app-relay-up', 'App · 中继可用但本页走 Cloudflare：Cloudflare 隧道（中继可用，但更慢）',
    directInApp('other', { path: 'relay', rtt_ms: 140 }, { direct: 'cancelled', public: 180, winner: 'public' })),
  directFrame('direct-app-tunnel', 'App · 没有直连路径：本设备正在使用：Cloudflare 隧道',
    directInApp('other', { preferred: 'tunnel' }, { direct: 'failed', public: 95, winner: 'public' })),
  directFrame('direct-app-timeout', 'App · 最近一次连接：直连超时，改走 Cloudflare',
    directInApp('other', { path: 'ipv6', path_scope: 'global' }, { direct: 'timeout', public: 1620, winner: 'public' })),
  directFrame('direct-page-unknown', '直连 · 已就绪，本页连接还没报告：不显示本页这一行', directOn('running', 'published')),
  directFrame('direct-awaiting-permission', '直连 · 等待授权', directOn('waiting-permission', 'owner_not_running', 'other')),
  directFrame('direct-denied', '直连 · 未获准', directOn('waiting-permission', 'permission_denied', 'other')),
  directFrame('direct-awaiting-certificate', '直连 · 等待证书', directOn('waiting-certificate', 'certificate_not_ready', 'other')),
  directFrame('direct-not-running', '直连 · 未运行（启动中、出错、未安装等）', directOn('error', 'transport_start_failed', 'other')),
]);

export default designFrames;
