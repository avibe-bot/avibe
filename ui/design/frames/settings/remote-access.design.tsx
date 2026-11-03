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

const designFrames = frames([
  { name: 'unpaired', title: '设置 · 远程访问 · 未配对', width: 1440, height: 900, status: 'shipped', route, fixtures: unpaired, render: page },
  { name: 'running', title: '设置 · 远程访问 · 已配对、运行中', width: 1440, height: 900, status: 'shipped', route, fixtures: paired, render: page },
  { name: 'automatic-wait', title: 'Remote access · automatic retry, manual available', width: 1440, height: 900, status: 'shipped', route, fixtures: automaticWait, render: page },
  { name: 'automatic-wait-phone', title: 'Remote access · automatic retry on mobile', width: 390, height: 1100, status: 'shipped', route, fixtures: automaticWait, render: page },
  { name: 'phone', title: '设置 · 远程访问 · 手机', width: 390, height: 844, status: 'shipped', route, fixtures: unpaired, render: page },
]);

export default designFrames;
