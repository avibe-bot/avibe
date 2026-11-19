// Proposed 2026-11-19 (frames only): the connection paths on Settings · Remote access
// (ui/design: settings/remote-access-paths/*). One card above the existing Remote access card:
// every path with its state, the path this device uses now, and, in the App only, a per-device
// choice between automatic and Cloudflare only. Draft copy, inline in both languages.
import { SettingsPanel } from '@/components/settings/SettingsPrimitives';
import { Badge } from '@/components/ui/badge';
import { SegmentedRadio } from '@/components/ui/segmented';
import { useDraft } from './draft';

export type PathKind = 'tunnel' | 'lan' | 'punched' | 'relay';

/** A path's state as the instance knows it. */
export type PathState =
  | 'running' | 'stopped'                 // Cloudflare 隧道: the connector
  | 'on' | 'off' | 'starting' | 'error'   // 局域网直连 / 打洞直连: the direct listener
  | 'connected' | 'disconnected';         // 中继: the instance's home relay

export type RemoteAccessPathsProps = {
  /** Where this page runs: the Avibe App (with the `direct` capability) or a browser. */
  view: 'app' | 'browser';
  states: Record<PathKind, PathState>;
  /** The path this device uses now, with its measured round trip if there is one. */
  current: { kind: PathKind; rttMs?: number } | null;
  /** The Cloudflare tunnel's measured quality, as the page already shows it. */
  tunnelQuality?: { grade: 'good' | 'fair' | 'poor'; latencyMs: number };
  /** The App's per-device choice. */
  choice?: 'auto' | 'tunnel';
  /** The App's direct path just ended and new connections went back to Cloudflare. */
  fellBack?: boolean;
};

const ORDER: PathKind[] = ['tunnel', 'lan', 'punched', 'relay'];

export function RemoteAccessPaths({ view, states, current, tunnelQuality, choice = 'auto', fellBack }: RemoteAccessPathsProps) {
  const d = useDraft();
  const name: Record<PathKind, string> = {
    tunnel: d('Cloudflare 隧道', 'Cloudflare tunnel'),
    lan: d('局域网直连', 'LAN direct'),
    punched: d('打洞直连', 'Hole-punched direct'),
    relay: d('中继', 'Relay'),
  };
  const state: Record<PathState, { label: string; variant: 'success' | 'warning' | 'destructive' | 'secondary' }> = {
    running: { label: d('运行中', 'Running'), variant: 'success' },
    stopped: { label: d('已停止', 'Stopped'), variant: 'secondary' },
    on: { label: d('已开启', 'On'), variant: 'success' },
    off: { label: d('未开启', 'Off'), variant: 'secondary' },
    starting: { label: d('准备中', 'Starting'), variant: 'warning' },
    error: { label: d('出错', 'Error'), variant: 'destructive' },
    connected: { label: d('已连接', 'Connected'), variant: 'success' },
    disconnected: { label: d('未连接', 'Not connected'), variant: 'secondary' },
  };
  const quality = tunnelQuality
    ? d(
      `${{ good: '良好', fair: '一般', poor: '较差' }[tunnelQuality.grade]} · ${tunnelQuality.latencyMs} ms`,
      `${{ good: 'Good', fair: 'Fair', poor: 'Poor' }[tunnelQuality.grade]} · ${tunnelQuality.latencyMs} ms`,
    )
    : null;

  const using = current
    ? d(`本设备正在使用：${name[current.kind]}`, `This device is using: ${name[current.kind]}`)
    : d('本设备的连接还没报告路径', 'This device has not reported its path yet');
  const note = view === 'browser'
    ? d('直连只在 Avibe App 里使用。', 'Direct paths are used by the Avibe App only.')
    : fellBack
      ? d('直连中断，新连接已自动改用 Cloudflare 隧道。', 'The direct path ended; new connections went back to the Cloudflare tunnel.')
      : choice === 'tunnel'
        ? d('本设备只用 Cloudflare 隧道。', 'This device uses only the Cloudflare tunnel.')
        : null;

  return (
    <SettingsPanel
      title={d('连接路径', 'Connection paths')}
      description={
        <>
          <span className="font-medium text-foreground">{using}</span>
          {note && <span className="mt-0.5 block">{note}</span>}
        </>
      }
      actions={view === 'app' && (
        <SegmentedRadio
          ariaLabel={d('本设备使用的路径', 'Path for this device')}
          value={choice}
          onChange={() => undefined}
          options={[
            { id: 'auto', label: d('自动', 'Automatic') },
            { id: 'tunnel', label: d('仅 Cloudflare', 'Cloudflare only') },
          ]}
        />
      )}
    >
      {ORDER.map((kind) => {
        const inUse = current?.kind === kind;
        return (
          <div
            key={kind}
            className={`flex items-center justify-between gap-3 border-b border-border px-5 py-3 last:border-b-0 ${inUse ? 'bg-mint/[0.05]' : ''}`}
          >
            <div className="flex min-w-0 items-center gap-2">
              <span className="text-[13px] font-medium text-foreground">{name[kind]}</span>
              {inUse && <Badge variant="info">{d('使用中', 'In use')}</Badge>}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {/* Quality only where it was measured: the tunnel's own probe, and the round trip
                  of the direct connection this App is using. */}
              {kind === 'tunnel' && quality && states.tunnel === 'running' && (
                <span className="text-[11px] tabular-nums text-muted">{quality}</span>
              )}
              {kind !== 'tunnel' && inUse && current?.rttMs !== undefined && (
                <span className="text-[11px] tabular-nums text-muted">{current.rttMs} ms</span>
              )}
              <Badge variant={state[states[kind]].variant}>{state[states[kind]].label}</Badge>
            </div>
          </div>
        );
      })}
    </SettingsPanel>
  );
}
