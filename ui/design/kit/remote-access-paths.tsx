// Proposed 2026-11-19 (frames only): the connection paths on Settings · Remote access
// (ui/design: settings/remote-access-paths/*). One card above the Remote access card, which then
// keeps only pairing, the avibe.bot link and the tunnel controls. Every fact appears once: the
// direct switch for this instance, the path this device uses now, every path with one state from
// four words, the measured number beside it where there is one, and, in the App only, a
// per-device choice. Draft copy, inline in both languages.
import { SettingsPanel } from '@/components/settings/SettingsPrimitives';
import { Badge } from '@/components/ui/badge';
import { SegmentedRadio } from '@/components/ui/segmented';
import { Switch } from '@/components/ui/switch';
import { useDraft } from './draft';

export type PathKind = 'tunnel' | 'lan' | 'punched' | 'relay';

/** A path's state from the instance: on, off, or an error. `In use` comes from the device. */
export type PathState = 'on' | 'off' | 'error';

export type RemoteAccessPathsProps = {
  /** Where this page runs: the Avibe App (with the `direct` capability) or a browser. */
  view: 'app' | 'browser';
  /** The instance's direct switch: off, on and getting ready, on, or it could not turn on. */
  direct: 'off' | 'preparing' | 'on' | 'failed';
  states: Record<PathKind, PathState>;
  /** The path this device uses now, with its measured round trip if there is one. */
  current: { kind: PathKind; rttMs?: number } | null;
  /** The Cloudflare tunnel's measured latency. */
  tunnelMs?: number;
  /** The App's per-device choice. */
  choice?: 'auto' | 'tunnel';
  /** The App's direct path just ended and new connections went back to Cloudflare. */
  fellBack?: boolean;
};

const ORDER: PathKind[] = ['tunnel', 'lan', 'punched', 'relay'];

export function RemoteAccessPaths({ view, direct, states, current, tunnelMs, choice = 'auto', fellBack }: RemoteAccessPathsProps) {
  const d = useDraft();
  const name: Record<PathKind, string> = {
    tunnel: d('Cloudflare 隧道', 'Cloudflare tunnel'),
    lan: d('局域网直连', 'LAN direct'),
    punched: d('打洞直连', 'Hole-punched direct'),
    relay: d('中继', 'Relay'),
  };
  const word: Record<PathState | 'inUse', { label: string; variant: 'info' | 'success' | 'secondary' | 'destructive' }> = {
    inUse: { label: d('使用中', 'In use'), variant: 'info' },
    on: { label: d('已开启', 'On'), variant: 'success' },
    off: { label: d('未开启', 'Off'), variant: 'secondary' },
    error: { label: d('出错', 'Error'), variant: 'destructive' },
  };
  const using = current
    ? d(`本设备正在使用：${name[current.kind]}`, `This device is using: ${name[current.kind]}`)
    : d('本设备的连接还没报告路径', 'This device has not reported its path yet');
  const note = view === 'browser'
    ? d('直连只在 Avibe App 里使用。', 'Direct paths are used by the Avibe App only.')
    : fellBack
      ? d('直连中断，新连接已自动改用 Cloudflare 隧道。', 'The direct path ended; new connections went back to the Cloudflare tunnel.')
      : null;
  const directLine = {
    off: null,
    preparing: d('正在准备：为这个实例的地址申请证书，并启动直连。', 'Getting ready: a certificate for this instance’s address, then direct.'),
    on: null,
    failed: d('没能开启：直连还没对这个实例开放。', 'Could not turn on: direct is not yet available for this instance.'),
  }[direct];

  return (
    <SettingsPanel
      title={d('连接路径', 'Connection paths')}
      description={
        <>
          <span className="block truncate font-medium text-foreground">{using}</span>
          {note && <span className="mt-0.5 block">{note}</span>}
        </>
      }
    >
      <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-3">
        <div className="min-w-0">
          <div className="text-[13px] font-medium text-foreground">{d('直连', 'Direct')}</div>
          <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
            {d(
              '手机能直接连到这台电脑时就直连；这个实例的地址会单独申请证书。',
              'Your phone connects to this computer directly when it can; this instance’s address gets its own certificate.',
            )}
          </div>
          {directLine && (
            <div className={`mt-1 text-[11px] ${direct === 'failed' ? 'text-destructive-ink' : 'text-gold-ink'}`}>{directLine}</div>
          )}
        </div>
        <Switch checked={direct !== 'off'} onCheckedChange={() => undefined} aria-label={d('直连', 'Direct')} />
      </div>
      {ORDER.map((kind) => {
        const inUse = current?.kind === kind;
        const shown = word[inUse ? 'inUse' : states[kind]];
        // The measured number, only where it was measured: the tunnel's own probe, and the round
        // trip of the connection this App is using.
        const ms = kind === 'tunnel' ? (states.tunnel === 'on' ? tunnelMs : undefined) : inUse ? current?.rttMs : undefined;
        return (
          <div
            key={kind}
            className={`flex items-center justify-between gap-3 border-b border-border px-5 py-3 last:border-b-0 ${inUse ? 'bg-cyan/[0.06]' : ''}`}
          >
            <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{name[kind]}</span>
            <div className="flex shrink-0 items-center gap-2">
              {ms !== undefined && <span className="text-[11px] tabular-nums text-muted">{ms} ms</span>}
              <Badge variant={shown.variant}>{shown.label}</Badge>
            </div>
          </div>
        );
      })}
      {view === 'app' && (
        <div className="flex items-center justify-between gap-3 px-5 py-3">
          <span className="text-[12px] text-muted">{d('本设备', 'This device')}</span>
          <SegmentedRadio
            ariaLabel={d('本设备使用的路径', 'Path for this device')}
            value={choice}
            onChange={() => undefined}
            options={[
              { id: 'auto', label: d('自动', 'Automatic') },
              { id: 'tunnel', label: d('仅 Cloudflare', 'Cloudflare only') },
            ]}
          />
        </div>
      )}
    </SettingsPanel>
  );
}
