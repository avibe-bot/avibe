// Proposed 2026-11-19 (frames only, no implementation), revised after Product's 09:10 review:
// Settings · Remote access shows the direct switch, every connection path with one state, the path
// this device uses now, and in the App a per-device choice, automatic by default. The new card
// (kit/remote-access-paths.tsx) sits above the Remote access card, which keeps pairing, the
// avibe.bot link and the tunnel controls (`pathsCard`); the running state, the quality and the
// direct row (#3940) live only in the new card.
// What each line comes from, and what is left out: the notes on the Show Page.
import { RemoteAccess } from '@/components/RemoteAccess';
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

const ready = { tunnel: 'on', lan: 'on', punched: 'on', relay: 'on' } as const;
const off = { tunnel: 'on', lan: 'off', punched: 'off', relay: 'off' } as const;
const broken = { tunnel: 'on', lan: 'error', punched: 'error', relay: 'error' } as const;

const PHONE = { width: 390, height: 1150 };
const DESKTOP = { width: 1440, height: 1000 };

function frame(
  name: string,
  title: string,
  size: { width: number; height: number },
  paths: RemoteAccessPathsProps,
  themes: FrameSpec['themes'] = ['light'],
): FrameSpec {
  return {
    name,
    title,
    ...size,
    status: 'proposed',
    themes,
    route,
    fixtures: fixtures(paths.view === 'app'),
    render: settingsPage('remote-access', (
      <div className="space-y-4">
        <RemoteAccessPaths {...paths} />
        <RemoteAccess pathsCard />
      </div>
    )),
  };
}

const designFrames = frames([
  // The App (iPhone): automatic by default.
  frame('app-off', 'App · 直连未开启：只有 Cloudflare 隧道', PHONE,
    { view: 'app', direct: 'off', states: off, current: { kind: 'tunnel' }, tunnelMs: 120 }),
  frame('app-preparing', 'App · 刚打开直连：正在准备', PHONE,
    { view: 'app', direct: 'preparing', states: off, current: { kind: 'tunnel' }, tunnelMs: 120 }),
  frame('app-failed', 'App · 直连没能开启', PHONE,
    { view: 'app', direct: 'failed', states: broken, current: { kind: 'tunnel' }, tunnelMs: 120 }),
  frame('app-punched', 'App · 正在使用打洞直连', PHONE,
    { view: 'app', direct: 'on', states: ready, current: { kind: 'punched', rttMs: 38 }, tunnelMs: 120 }, ['light', 'dark']),
  frame('app-cloudflare-only', 'App · 本设备选了「仅 Cloudflare」', PHONE,
    { view: 'app', direct: 'on', states: ready, current: { kind: 'tunnel' }, tunnelMs: 120, choice: 'tunnel' }),
  frame('app-fallback', 'App · 直连中断，已自动改用 Cloudflare 隧道', PHONE,
    { view: 'app', direct: 'on', states: ready, current: { kind: 'tunnel' }, tunnelMs: 120, fellBack: true }),
  // A browser never uses a direct path, and has no per-device choice.
  frame('browser-desktop', '浏览器 · 桌面宽度 · 直连已开启', DESKTOP,
    { view: 'browser', direct: 'on', states: ready, current: { kind: 'tunnel' }, tunnelMs: 120 }),
]);

export default designFrames;
