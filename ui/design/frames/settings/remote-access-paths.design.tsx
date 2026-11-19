// Proposed 2026-11-19 (frames only, no implementation): Settings · Remote access shows every
// connection path, each path's state, the path this device uses now, and in the App a per-device
// choice, automatic by default. The new card (kit/remote-access-paths.tsx) sits above the shipped
// Remote access card, which these frames draw unchanged. In the implementation the new card takes
// over that card's direct row (#3940) and its quality cell, so neither is said twice.
// What each line comes from, and what is left out: the notes on the Show Page.
import { RemoteAccessPage } from '@/components/RemoteAccessPage';
import type { RemoteAccessStatus, TunnelQualitySnapshot } from '@/context/ApiContext';
import { DESIGN_NOW } from '../../shell/clock';
import { frames, type FrameSpec } from '../../frame';
import { IOS_APP_CAPABILITIES } from '../../shell/fixtures';
import { RemoteAccessPaths, type RemoteAccessPathsProps } from '../../kit/remote-access-paths';
import { settingsConfig, settingsFixtures, settingsPage } from './fixtures';

const route = '/settings/remote-access';

const pairedConfig = {
  ...settingsConfig,
  remote_access: {
    ...settingsConfig.remote_access,
    vibe_cloud: { ...settingsConfig.remote_access.vibe_cloud, enabled: true, paired: true, public_url: 'https://alex-app.avibe.bot' },
  },
};

const quality: TunnelQualitySnapshot = {
  schema_version: 2, state: 'healthy', grade: 'good', sampled_at: new Date(DESIGN_NOW).toISOString(),
  protocol: 'quic', connector_count: 1, ha_connections: 4, rtt_ms: { min: 110, median: 120, max: 160 },
  baseline_median_rtt_ms: 120, edge_locations: ['hkg01'], window_seconds: 60,
  request_errors_per_minute: 0, packet_loss_per_minute: 0,
  recovery: { state: 'idle', last_attempt_at: null, last_trigger: null, last_result: null, previous_median_rtt_ms: null,
    result_median_rtt_ms: null, next_attempt_at: null, attempt_count_window: 0 },
} as unknown as TunnelQualitySnapshot;

// The shipped card, as a paired instance with a running tunnel and no direct row (the new card
// carries it).
const fixtures = (app: boolean) => settingsFixtures({
  '/api/config': pairedConfig,
  '/api/remote-access/status': {
    ok: true, enabled: true, paired: true, running: true, public_url: 'https://alex-app.avibe.bot',
    pid_state: 'running', transport_protocol: 'auto', tunnel_quality: quality,
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: '4', edge_bind_address: '' },
  } satisfies RemoteAccessStatus,
  '/api/remote-access/network-interfaces': { ok: true, interfaces: [] },
}, app ? { nativeShell: { ...IOS_APP_CAPABILITIES, direct: 1 } } : {});

const tunnelQuality = { grade: 'good', latencyMs: 120 } as const;
const ready = { tunnel: 'running', lan: 'on', punched: 'on', relay: 'connected' } as const;
const off = { tunnel: 'running', lan: 'off', punched: 'off', relay: 'off' } as const;

const PHONE = { width: 390, height: 1100 };
const DESKTOP = { width: 1440, height: 1000 };
const IPAD = { width: 1024, height: 1000 };

function frame(name: string, title: string, size: { width: number; height: number }, paths: RemoteAccessPathsProps): FrameSpec {
  return {
    name,
    title,
    ...size,
    status: 'proposed',
    themes: ['light', 'dark'],
    route,
    fixtures: fixtures(paths.view === 'app'),
    render: settingsPage('remote-access', (
      <div className="space-y-4">
        <RemoteAccessPaths {...paths} />
        <RemoteAccessPage />
      </div>
    )),
  };
}

const designFrames = frames([
  // The App (iPhone): automatic by default.
  frame('app-off', 'App · 直连未开启：只有 Cloudflare 隧道', PHONE,
    { view: 'app', states: off, current: { kind: 'tunnel' }, tunnelQuality }),
  frame('app-ready', 'App · 直连已就绪，本设备的连接还在建立：先走 Cloudflare 隧道', PHONE,
    { view: 'app', states: ready, current: { kind: 'tunnel' }, tunnelQuality }),
  frame('app-lan', 'App · 正在使用局域网直连（往返时间由 App 实测）', PHONE,
    { view: 'app', states: ready, current: { kind: 'lan', rttMs: 4 }, tunnelQuality }),
  frame('app-punched', 'App · 正在使用打洞直连', PHONE,
    { view: 'app', states: ready, current: { kind: 'punched', rttMs: 38 }, tunnelQuality }),
  frame('app-relay', 'App · 正在使用中继', PHONE,
    { view: 'app', states: ready, current: { kind: 'relay', rttMs: 210 }, tunnelQuality }),
  frame('app-cloudflare-only', 'App · 本设备选了「仅 Cloudflare」', PHONE,
    { view: 'app', states: ready, current: { kind: 'tunnel' }, tunnelQuality, choice: 'tunnel' }),
  frame('app-fallback', 'App · 直连中断，已自动改用 Cloudflare 隧道', PHONE,
    { view: 'app', states: ready, current: { kind: 'tunnel' }, tunnelQuality, fellBack: true }),
  frame('app-error', 'App · 实例上的直连出错：只剩 Cloudflare 隧道', PHONE,
    { view: 'app', states: { tunnel: 'running', lan: 'error', punched: 'error', relay: 'disconnected' }, current: { kind: 'tunnel' }, tunnelQuality }),
  frame('app-relay-ipad', 'App · iPad 宽度 · 正在使用中继', IPAD,
    { view: 'app', states: ready, current: { kind: 'relay', rttMs: 210 }, tunnelQuality }),
  // A browser never uses a direct path.
  frame('browser-phone', '浏览器 · 手机宽度 · 直连已就绪，本设备走 Cloudflare 隧道；没有手动选择', PHONE,
    { view: 'browser', states: ready, current: { kind: 'tunnel' }, tunnelQuality }),
  frame('browser-desktop', '浏览器 · 桌面宽度 · 直连已就绪', DESKTOP,
    { view: 'browser', states: ready, current: { kind: 'tunnel' }, tunnelQuality }),
  frame('browser-desktop-off', '浏览器 · 桌面宽度 · 直连未开启', DESKTOP,
    { view: 'browser', states: off, current: { kind: 'tunnel' }, tunnelQuality }),
]);

export default designFrames;
