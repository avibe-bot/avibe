// Settings · Remote access: pair this machine with Avibe Cloud and reach it from anywhere.
import { RemoteAccessPage } from '@/components/RemoteAccessPage';
import type { RemoteAccessStatus, TunnelQualitySnapshot } from '@/context/ApiContext';
import { DESIGN_NOW } from '../../shell/clock';

import { frames } from '../../frame';
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

const designFrames = frames([
  { name: 'unpaired', title: '设置 · 远程访问 · 未配对', width: 1440, height: 900, status: 'shipped', route, fixtures: unpaired, render: page },
  { name: 'running', title: '设置 · 远程访问 · 已配对、运行中', width: 1440, height: 900, status: 'shipped', route, fixtures: paired, render: page },
  { name: 'automatic-wait', title: 'Remote access · automatic retry, manual available', width: 1440, height: 900, status: 'shipped', route, fixtures: automaticWait, render: page },
  { name: 'automatic-wait-phone', title: 'Remote access · automatic retry on mobile', width: 390, height: 1100, status: 'shipped', route, fixtures: automaticWait, render: page },
  { name: 'phone', title: '设置 · 远程访问 · 手机', width: 390, height: 844, status: 'shipped', route, fixtures: unpaired, render: page },
  directFrame('direct-page-direct', '直连 · 已就绪（证书未过期才显示日期），本页经直连', directOn('running', 'published', 'direct')),
  directFrame('direct-page-other', '直连 · 已就绪，本页未走直连', directOn('running', 'published', 'other')),
  directFrame('direct-page-unknown', '直连 · 已就绪，本页连接还没报告：不显示本页这一行', directOn('running', 'published')),
  directFrame('direct-awaiting-permission', '直连 · 等待授权', directOn('waiting-permission', 'owner_not_running', 'other')),
  directFrame('direct-denied', '直连 · 未获准', directOn('waiting-permission', 'permission_denied', 'other')),
  directFrame('direct-awaiting-certificate', '直连 · 等待证书', directOn('waiting-certificate', 'certificate_not_ready', 'other')),
  directFrame('direct-not-running', '直连 · 未运行（启动中、出错、未安装等）', directOn('error', 'transport_start_failed', 'other')),
]);

export default designFrames;
