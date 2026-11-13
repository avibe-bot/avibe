// One update panel, one set of states. Each desktop frame shows the same computer's
// update from the three places it is read: the desktop app's account footer (its own
// updater), the local Web UI and the remote Web UI through avibe.bot (both read the
// host's Runtime relay, `/api/version`). Each phone frame shows the remote Web UI, as
// a phone reaches the host. The panel is the shipped `UpdatePanel`; each column's
// model comes from that surface's own source through the shipped mapping.
import type { DesktopRemoteUpdateRequest, VersionInfo } from '@/context/ApiContext';
import type { DesktopUpdateState } from '@/lib/desktopShell';

import { frames, type FrameSpec } from '../../frame';
import { IPHONE_17_PRO } from '../../shell/fixtures';
import { Phone, Surfaces, TrustedView, type SurfaceState } from './surfaces';

// The design clock reads 2026-11-01 08:00Z, so this check was eight minutes ago.
const CHECKED_AT = '2026-11-01T07:52:00Z';
const OFFERED = '3.2.7-rc.1';

const native = (state: Partial<DesktopUpdateState>): DesktopUpdateState => ({
  phase: 'current', latest: null, channel: 'test', prereleases: true, skippedVersion: null,
  interactive: true, remoteRequest: null, checkedAt: CHECKED_AT, autoUpdate: false,
  hostActions: ['check', 'prereleases', 'auto_update'], ...state,
});

const receipt = (phase: DesktopRemoteUpdateRequest['phase'], extra: Partial<DesktopRemoteUpdateRequest> = {}): DesktopRemoteUpdateRequest => ({
  request_id: 'request-1', target: OFFERED, phase, progress: null, error_code: null,
  updated_at: CHECKED_AT, ...extra,
});

const relay = (info: Partial<VersionInfo>): VersionInfo => ({
  current: '3.2.6', latest: null, has_update: false, error: null, managed_by: 'desktop',
  phase: 'current', desktop_version: '3.2.6', prereleases: true, auto_update: false, checked_at: CHECKED_AT,
  host_actions: ['check', 'prereleases', 'auto_update', 'install'], control_request: null,
  remote_install_allowed: false, remote_request: null, build: { kind: 'package' }, ...info,
});

type State = SurfaceState & { name: string; title: string };

const states: State[] = [
  {
    name: 'checking', title: '检查中',
    desktop: native({ phase: 'checking' }),
    web: relay({
      phase: 'checking',
      control_request: { request_id: 'check-1', action: 'check', enabled: null, phase: 'checking', error_code: null, updated_at: CHECKED_AT },
    }),
  },
  {
    name: 'current', title: '已是最新（自动更新开启）',
    desktop: native({ phase: 'current', autoUpdate: true }),
    web: relay({ phase: 'current', auto_update: true }),
  },
  {
    name: 'available', title: '有新版本（预览版开启）',
    desktop: native({ phase: 'available', latest: OFFERED }),
    web: relay({ phase: 'available', latest: OFFERED, has_update: true }),
  },
  {
    name: 'downloading', title: '下载中 42%',
    desktop: native({
      phase: 'downloading', latest: OFFERED,
      remoteRequest: { requestId: 'request-1', target: OFFERED, phase: 'downloading', progress: 42, errorCode: null, updatedAt: CHECKED_AT },
    }),
    web: relay({ phase: 'available', latest: OFFERED, has_update: true, remote_request: receipt('downloading', { progress: 42 }) }),
  },
  {
    name: 'installing', title: '安装中',
    desktop: native({ phase: 'installing', latest: OFFERED }),
    web: relay({ phase: 'available', latest: OFFERED, has_update: true, remote_request: receipt('installing') }),
  },
  {
    name: 'restarting', title: '安装完成，重新启动',
    desktop: native({ phase: 'installed', latest: OFFERED }),
    web: relay({ phase: 'available', latest: OFFERED, has_update: true, remote_request: receipt('restarting') }),
  },
  {
    name: 'updated', title: '已更新',
    desktop: native({ phase: 'current' }),
    desktopVersion: OFFERED,
    web: relay({ current: '3.2.7rc1', desktop_version: OFFERED, phase: 'current', remote_request: receipt('completed') }),
  },
  {
    name: 'check-failed', title: '检查失败',
    desktop: native({ phase: 'error' }),
    web: relay({ phase: 'error', error: "The desktop app's last update check failed." }),
  },
  {
    name: 'update-failed', title: '安装失败（旧版本保留）',
    desktop: native({
      phase: 'error',
      remoteRequest: { requestId: 'request-1', target: OFFERED, phase: 'failed', progress: null, errorCode: 'install_failed', updatedAt: CHECKED_AT },
    }),
    web: relay({ phase: 'available', latest: OFFERED, has_update: true, remote_request: receipt('failed', { error_code: 'install_failed' }) }),
  },
  {
    name: 'disabled', title: '此版本不能自动更新',
    desktop: native({ phase: 'disabled' }),
    web: relay({ phase: 'disabled' }),
  },
  {
    name: 'old-desktop', title: '例外：运行 Avibe 的电脑上的桌面应用太旧（3.2.6 及更早）',
    web: relay({ phase: 'available', latest: OFFERED, has_update: true, host_actions: undefined, auto_update: null }),
  },
  {
    name: 'no-result', title: '桌面应用刚启动、第一次检查还没完成（短暂状态）',
    desktop: native({ phase: 'idle', checkedAt: null }),
    web: relay({ phase: null, desktop_version: null, prereleases: null, auto_update: null, checked_at: null, host_actions: undefined }),
  },
];

const designFrames = frames([...states.flatMap((state): FrameSpec[] => [
  {
    name: state.name,
    title: `更新面板 · ${state.title} · 桌面应用 / 本机 Web / 远程 Web`,
    width: 1136,
    height: 720,
    status: 'approved',
    render: () => <Surfaces state={state} />,
  },
  {
    name: `${state.name}-phone`,
    title: `更新面板 · ${state.title} · 手机（远程 Web）`,
    width: IPHONE_17_PRO.width,
    height: 800,
    status: 'approved',
    render: () => <Phone state={state} />,
  },
]), {
  name: 'trusted-view',
  title: '更新面板 · 桌面应用连接到另一台电脑时：本机更新在受信界面里打开，同一块面板',
  width: 1136,
  height: 720,
  status: 'approved',
  render: () => <TrustedView desktop={native({ phase: 'available', latest: OFFERED })} />,
}]);

export default designFrames;