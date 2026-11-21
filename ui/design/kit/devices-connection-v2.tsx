// Proposed 2026-11-21 (frames only): 「连接方式」 after the 07:51 decisions (ui/design:
// settings/devices-connection-v2/*), replacing the keep-or-put-back trial of `ConnectionPageView`.
// The instance verifies a tunnel change itself: there is no keep button and no countdown, only a
// status line (applying, verifying, then applied or set back). Direct and the relay are ordinary
// switches, refused with one sentence when the change could cut the person off. The relay row exists
// only on an instance enrolled in the relay trial from the host's command line. At the bottom, for
// the owner: the domain, its rename, and binding remote access again. Contract:
// docs/plans/devices-connection-settings.md (#4114). Draft copy, inline in both languages.
import type { ReactNode } from 'react';
import { Check, ChevronRight, CircleHelp, History, LoaderCircle, Route, Stethoscope, TriangleAlert, Undo2 } from 'lucide-react';

import { BackToDevices } from '@/components/devices/DevicesPageView';
import {
  TunnelAutoRecoverySwitch, TunnelIpFamilyControl, TunnelOutboundSelect, TunnelProfileControl, TunnelProtocolControl,
} from '@/components/remoteAccess/TunnelControls';
import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '@/components/settings/SettingsPrimitives';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useDraft } from './draft';
import { Para } from './devices-text';
import { RebindRow } from './devices-rename';

type D = (zh: string, en: string) => string;
type Setting = 'protocol' | 'ipFamily' | 'outbound';
type Value = { zh: string; en: string };

/** The latest tunnel change of one setting, as `operation` / `outcome` report it, or a response
 *  that never arrived. */
export type TunnelChange =
  /** `operation`: still in progress. */
  | { setting: Setting; state: 'applying' | 'verifying' }
  | { setting: Setting; state: 'restoring'; from: Value }
  | { setting: Setting; state: 'applied' | 'superseded' }
  /** It did not connect (or could not be checked) and the previous value is back. */
  | { setting: Setting; state: 'restored'; reason: 'verification_failed' | 'verification_unavailable'; from: Value }
  /** Setting it back is unfinished, or the change never took effect at all. */
  | {
    setting: Setting; state: 'failed'; from: Value;
    reason: 'saved_restored_runtime_pending' | 'storage_restore_failed' | 'candidate_not_ready' | 'connection_change_busy' | 'settings_not_applied';
  }
  /** The response was lost (the switch can drop the very connection it came over). */
  | { setting: Setting; state: 'unknown' };

export type DirectState = 'ready' | 'off' | 'denied' | 'awaiting-permission' | 'awaiting-certificate' | 'not-running';

export type ConnectionV2Props = {
  /** Where the page is open: at the computer itself, or a browser / the App elsewhere. */
  viewer: 'local' | 'remote';
  direct: DirectState;
  /** The relay row: absent unless this instance is enrolled from the host's command line.
   *  `unavailable`: why it cannot be turned on now; the switch is then disabled, with that reason. */
  relay?: { enabled: boolean; connected: boolean; unavailable?: 'direct_permission_required' | 'relay_transport_unavailable' };
  /** The tunnel's own state, and which path this page's connection came through. */
  tunnel?: 'up' | 'down';
  via?: 'tunnel' | 'direct';
  change?: TunnelChange;
  /** A switch was refused, said in one line under it. */
  refused?: { row: 'direct' | 'relay'; reason: 'tunnel_unhealthy' | 'connection_change_busy' };
  /** Fixed values that could not connect for 3 minutes were set back to automatic. */
  returnedToAutomatic?: { at: Value; changes: Array<{ setting: Setting; from: Value }> };
  protocol: 'auto' | 'quic' | 'http2';
  /** The owner's domain at the bottom, with 「更改域名」 and the quiet 「重新绑定」 row (at the computer). */
  host?: string;
  /** The state cannot be trusted right now (`503 connection_state_unavailable`): no settings, one line. */
  unavailable?: boolean;
};

const settingName = (d: D): Record<Setting, string> => ({
  protocol: d('传输协议', 'Transport protocol'),
  ipFamily: d('边缘网络 IP', 'Edge IP family'),
  outbound: d('出口网卡', 'Outbound interface'),
});

/** One status line for a tunnel change, in each state the instance can report. */
export function ChangeLine({ change }: { change: TunnelChange }) {
  const d = useDraft();
  const pick = (value: Value) => d(value.zh, value.en);
  const busy = ['applying', 'verifying', 'restoring'].includes(change.state);
  const [Icon, tone, text]: [typeof Check, string, string] = (() => {
    switch (change.state) {
      case 'applying': return [LoaderCircle, 'text-muted', d('正在应用', 'Applying')];
      case 'verifying': return [LoaderCircle, 'text-muted', d('验证中', 'Verifying')];
      case 'restoring': return [LoaderCircle, 'text-muted', d(`正在改回「${pick(change.from)}」`, `Setting back to ${pick(change.from)}`)];
      case 'applied': return [Check, 'text-mint-ink', d('已生效', 'In effect')];
      case 'superseded': return [History, 'text-muted', d('被后来的更改取代了', 'Replaced by a later change')];
      case 'restored': return change.reason === 'verification_failed'
        ? [Undo2, 'text-gold-ink', d(`没连上，已改回「${pick(change.from)}」`, `Did not connect; set back to ${pick(change.from)}`)]
        : [Undo2, 'text-gold-ink', d(`无法验证（改之前隧道就没有正常连上），已改回「${pick(change.from)}」`, `Could not be checked (the tunnel was not working before the change); set back to ${pick(change.from)}`)];
      case 'failed':
        switch (change.reason) {
          case 'saved_restored_runtime_pending': return [TriangleAlert, 'text-gold-ink', d(`已改回「${pick(change.from)}」，隧道还在恢复`, `Set back to ${pick(change.from)}; the tunnel is still coming back`)];
          case 'storage_restore_failed': return [TriangleAlert, 'text-destructive-ink', d('没能改回原来的设置。请在运行 Avibe 的电脑上检查。', 'Could not set it back. Check it on the computer running Avibe.')];
          case 'candidate_not_ready': return [Undo2, 'text-gold-ink', d(`没有改：新的连接没有建立起来，仍是「${pick(change.from)}」。`, `Not changed: the new connection did not come up; it is still ${pick(change.from)}.`)];
          case 'connection_change_busy': return [Undo2, 'text-gold-ink', d(`没有改：另一项更改还在进行，仍是「${pick(change.from)}」。`, `Not changed: another change is in progress; it is still ${pick(change.from)}.`)];
          case 'settings_not_applied': return [Undo2, 'text-gold-ink', d(`没有改：设置没能应用，仍是「${pick(change.from)}」。`, `Not changed: the setting could not be applied; it is still ${pick(change.from)}.`)];
        }
        break;
      case 'unknown': return [CircleHelp, 'text-muted', d('没收到结果。重新连上后会自动读取结果，不会重复这次更改。', 'No result received. Once reconnected, Avibe reads the result again and does not repeat the change.')];
    }
    return [CircleHelp, 'text-muted', ''];
  })();
  return (
    <div role="status" className={`mt-1 flex items-start gap-1.5 text-[11px] leading-relaxed ${tone}`}>
      <Icon className={`mt-0.5 size-3.5 shrink-0 ${busy ? 'motion-safe:animate-spin' : ''}`} />
      <span><Para text={text} /></span>
    </div>
  );
}

function Row({ title, hint, status, control, tone }: { title: string; hint?: ReactNode; status?: ReactNode; control?: ReactNode; tone?: 'info' }) {
  return (
    <div className={`flex flex-col gap-2 border-b border-border px-5 py-3 last:border-b-0 md:flex-row md:items-center md:justify-between md:gap-6 ${tone === 'info' ? 'bg-cyan/[0.06]' : ''}`}>
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-foreground">{title}</div>
        {hint && <div className="mt-0.5 text-[11px] leading-relaxed text-muted">{typeof hint === 'string' ? <Para text={hint} /> : hint}</div>}
        {status}
      </div>
      {control && <div className="shrink-0">{control}</div>}
    </div>
  );
}

function Refused({ row, reason }: { row: 'direct' | 'relay'; reason: 'tunnel_unhealthy' | 'connection_change_busy' }) {
  const d = useDraft();
  return (
    <div role="alert" className="mt-1 flex items-start gap-1.5 text-[11px] leading-relaxed text-gold-ink">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
      <span>
        <Para text={reason === 'tunnel_unhealthy'
          ? row === 'direct'
            ? d('没有改：现在确认不了 Cloudflare 隧道是通的，关掉可能让你断开。稍后再试。', 'Not changed: it cannot be confirmed right now that the Cloudflare tunnel works, so turning this off could cut you off. Try again later.')
            : d('没有改：现在确认不了 Cloudflare 隧道是通的，改了可能让你断开。稍后再试。', 'Not changed: it cannot be confirmed right now that the Cloudflare tunnel works, so changing this could cut you off. Try again later.')
          : d('没有改：另一项更改还在进行，稍后再试。', 'Not changed: another change is still in progress. Try again shortly.')} />
      </span>
    </div>
  );
}

const directBadge = (d: D, state: DirectState): { label: string; variant: 'success' | 'secondary' | 'warning' } => ({
  ready: { label: d('可用', 'Available'), variant: 'success' as const },
  off: { label: d('未开启', 'Off'), variant: 'secondary' as const },
  denied: { label: d('未获准', 'Not allowed'), variant: 'warning' as const },
  'awaiting-permission': { label: d('等待授权', 'Awaiting permission'), variant: 'warning' as const },
  'awaiting-certificate': { label: d('等待证书', 'Awaiting certificate'), variant: 'warning' as const },
  'not-running': { label: d('未运行', 'Not running'), variant: 'warning' as const },
})[state];

const directWhy = (d: D, state: DirectState) => ({
  ready: null,
  off: null,
  denied: d('直连还没有对这个 Avibe 开放。', 'Direct is not yet available for this Avibe.'),
  'awaiting-permission': d('正在向 avibe.bot 申请为这个 Avibe 开放直连。', 'Asking avibe.bot to allow direct for this Avibe.'),
  'awaiting-certificate': d('正在为这个域名申请证书，好了会自动开始。', 'Getting a certificate for this domain; it starts by itself when ready.'),
  'not-running': d('直连组件没有运行。重启 Avibe 后再看。', 'The direct component is not running. Restart Avibe and look again.'),
})[state];

export function ConnectionV2({ viewer, direct, relay, change, refused, returnedToAutomatic, protocol, tunnel = 'up', via = 'tunnel', host, unavailable }: ConnectionV2Props) {
  const d = useDraft();
  if (unavailable) {
    return (
      <SettingsPageShell
        title={d('连接方式', 'Connection')}
        subtitle={d('Avibe 会自动走最快的那条路，断了自动换。这里一般不用动。', 'Avibe uses the fastest path by itself and switches when one fails. You rarely need to change anything here.')}
        activeTab="remote-access"
      >
        <BackToDevices />
        <SettingsPanel>
          <div role="alert" className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between">
            <p className="text-[13px] leading-relaxed text-foreground">
              <Para text={d('现在读不准连接方式的状态，先不显示这些设置。稍后再试。', 'The connection state cannot be read reliably right now, so these settings are not shown. Try again shortly.')} />
            </p>
            <Button variant="secondary" size="sm" className="shrink-0">{d('重试', 'Retry')}</Button>
          </div>
        </SettingsPanel>
      </SettingsPageShell>
    );
  }
  const pick = (value: Value) => d(value.zh, value.en);
  const statusFor = (setting: Setting) => (change?.setting === setting ? <ChangeLine change={change} /> : null);
  const pending = change && ['applying', 'verifying', 'restoring'].includes(change.state);
  const badge = directBadge(d, direct);
  const why = directWhy(d, direct);
  const pathRow = (title: string, hint: string) => (
    <Row
      title={title}
      hint={why ?? hint}
      control={<Badge variant={badge.variant}>{badge.label}</Badge>}
    />
  );
  return (
    <SettingsPageShell
      title={d('连接方式', 'Connection')}
      subtitle={d('Avibe 会自动走最快的那条路，断了自动换。这里一般不用动。', 'Avibe uses the fastest path by itself and switches when one fails. You rarely need to change anything here.')}
      activeTab="remote-access"
    >
      <BackToDevices />
      {returnedToAutomatic && (
        <div role="status" className="flex gap-3 rounded-xl border border-gold/40 bg-gold/10 px-5 py-3">
          <History className="mt-0.5 size-4 shrink-0 text-gold-ink" />
          <p className="text-[12px] leading-relaxed text-foreground">
            <Para text={d(
              `${pick(returnedToAutomatic.at)}，Cloudflare 隧道连续 3 分钟连不上，固定的设置已改回自动：${returnedToAutomatic.changes.map((each) => `${settingName(d)[each.setting]}（原来是「${pick(each.from)}」）`).join('、')}。`,
              `${pick(returnedToAutomatic.at)}: the Cloudflare tunnel could not connect for 3 minutes, so fixed settings went back to Automatic: ${returnedToAutomatic.changes.map((each) => `${settingName(d)[each.setting]} (was ${pick(each.from)})`).join(', ')}.`,
            )} />
          </p>
        </div>
      )}

      <SettingsPanel title={d('这个 Avibe 提供的路径', 'Paths this Avibe offers')}>
        <Row
          title={d('Cloudflare 隧道', 'Cloudflare tunnel')}
          hint={d('所有设备都能走，也是其他路径的后备。不能关闭。', 'Every device can use it, and it backs up the other paths. It cannot be turned off.')}
          control={tunnel === 'down'
            ? <Badge variant="warning">{d('没连上', 'Not connected')}</Badge>
            : <Badge variant={viewer === 'remote' && via === 'tunnel' ? 'info' : 'success'}>{viewer === 'remote' && via === 'tunnel' ? d('使用中', 'In use') : d('可用', 'Available')}</Badge>}
          tone={viewer === 'remote' && via === 'tunnel' && tunnel === 'up' ? 'info' : undefined}
        />
        <Row
          title={d('直连', 'Direct')}
          hint={(
            <>
              <Para text={d('Avibe App 能直接连到这台电脑时就不绕路。关掉后都走 Cloudflare 隧道。', 'The Avibe App goes straight to this computer when it can. When off, everything uses the Cloudflare tunnel.')} />
              <br />
              <Para text={d(
                '关掉直连后，只能靠直连连上的手机会断开，换到能连 Cloudflare 的网络才恢复。',
                'With direct off, a phone that can only connect through direct is cut off until it moves to a network that can reach Cloudflare.',
              )} />
            </>
          )}
          status={refused?.row === 'direct' ? <Refused row="direct" reason={refused.reason} /> : null}
          control={(
            <div className="flex items-center gap-2.5">
              {via === 'direct' && <Badge variant="info">{d('使用中', 'In use')}</Badge>}
              <ToggleSwitch enabled={direct !== 'off'} onClick={() => undefined} label={d('直连', 'Direct')} />
            </div>
          )}
          tone={via === 'direct' ? 'info' : undefined}
        />
        {direct !== 'off' && pathRow(d('局域网直连', 'Direct (local network)'), d('和这台电脑在同一网络时。', 'On the same network as this computer.'))}
        {direct !== 'off' && pathRow(d('打洞直连', 'Direct (hole punching)'), d('不在同一网络时，两边直接相连。', 'On different networks, the two sides connect directly.'))}
        {relay && (
          <Row
            title={d('中继', 'Relay')}
            hint={d(
              '打洞不通时经中继转发，通常比 Cloudflare 隧道慢。开着中继时，直连只有所有者能用。',
              'Forwards through a relay when hole punching fails; usually slower than the Cloudflare tunnel. While the relay is on, only the owner can use direct.',
            )}
            status={refused?.row === 'relay' ? <Refused row="relay" reason={refused.reason} /> : relay.unavailable ? (
              <div className="mt-1 text-[11px] leading-relaxed text-gold-ink">
                <Para text={relay.unavailable === 'direct_permission_required'
                  ? d('暂时开不了：这个 Avibe 还没有拿到直连的许可。', 'Cannot be turned on yet: this Avibe does not have permission for direct.')
                  : d('暂时开不了：现在的直连设置用不了中继。', 'Cannot be turned on right now: the current direct setup cannot use the relay.')} />
              </div>
            ) : relay.enabled && direct !== 'off' ? (
              <div className="mt-1 text-[11px] text-muted">{relay.connected ? d('已连上中继', 'Connected to the relay') : d('还没连上中继', 'Not connected to the relay yet')}</div>
            ) : null}
            control={<ToggleSwitch enabled={relay.enabled} disabled={Boolean(relay.unavailable)} onClick={() => undefined} label={d('中继', 'Relay')} />}
          />
        )}
      </SettingsPanel>

      <SettingsPanel
        title={d('Cloudflare 隧道设置', 'Cloudflare tunnel settings')}
        description={<Para text={d(
          '改了之后，Avibe 会自己验证隧道连不连得上；连不上就改回原来的设置。',
          'After a change, Avibe checks by itself that the tunnel connects; if it does not, the previous setting comes back.',
        )} />}
      >
        <Row
          title={d('传输协议', 'Transport protocol')}
          hint={protocol === 'auto' ? d('自动 · 当前 QUIC', 'Automatic · now QUIC') : d('固定后，网络变了不会自己换协议；连续 3 分钟连不上会改回自动。', 'Once fixed, it does not switch when the network changes; after 3 minutes without a connection it goes back to Automatic.')}
          status={statusFor('protocol')}
          control={<TunnelProtocolControl value={protocol} onChange={() => undefined} disabled={pending} />}
        />
        <Row title={d('边缘网络 IP', 'Edge IP family')} status={statusFor('ipFamily')} control={<TunnelIpFamilyControl value="4" onChange={() => undefined} disabled={pending} />} />
        <Row title={d('出口网卡', 'Outbound interface')} status={statusFor('outbound')} control={<TunnelOutboundSelect value="" onChange={() => undefined} interfaces={[]} className="md:w-56" disabled={pending} />} />
        <Row title={d('自动优化线路', 'Automatic route optimization')} control={<TunnelAutoRecoverySwitch value onChange={() => undefined} />} />
        <Row title={d('优化策略', 'Optimization policy')} control={<TunnelProfileControl value="balanced" onChange={() => undefined} />} />
      </SettingsPanel>

      <SettingsPanel>
        {[[Route, d('优化线路', 'Optimize route')], [Stethoscope, d('连接诊断与网络路径', 'Diagnostics and network path')]].map(([Icon, label]) => {
          const I = Icon as typeof Route;
          return (
            <div key={label as string} className="flex items-center gap-3 border-b border-border px-5 py-3 text-[13px] text-foreground last:border-b-0">
              <I className="size-4 shrink-0 text-muted" /><span className="flex-1">{label as string}</span><ChevronRight className="size-4 text-muted" />
            </div>
          );
        })}
      </SettingsPanel>

      {host && (
        <SettingsPanel>
          <div className="flex items-center gap-3 px-5 py-3">
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-foreground">{d('域名', 'Domain')}</div>
              <div className="mt-0.5 break-all font-mono text-[11px] text-muted">{host}</div>
            </div>
            <Button variant="ghost" size="compact" className="shrink-0 text-destructive-ink">{d('更改域名', 'Change the domain')}</Button>
          </div>
          {viewer === 'local' && <RebindRow />}
        </SettingsPanel>
      )}
    </SettingsPageShell>
  );
}
