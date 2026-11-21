// Proposed 2026-11-21 (frames only): 「连接方式」 after the 07:51 decisions (ui/design:
// settings/devices-connection-v2/*), replacing the keep-or-put-back trial of `ConnectionPageView`.
// The instance verifies a tunnel change itself: there is no keep button and no countdown, only a
// status line (verifying, then applied or put back). Direct and the relay are ordinary switches,
// refused with one sentence when turning them would cut the person off. The relay row exists only
// on an instance enrolled in the relay trial from the host's command line. Contract:
// docs/plans/devices-connection-settings.md. Draft copy, inline in both languages.
import type { ReactNode } from 'react';
import { Check, ChevronRight, History, LoaderCircle, Route, Stethoscope, TriangleAlert, Undo2 } from 'lucide-react';

import { BackToDevices } from '@/components/devices/DevicesPageView';
import {
  TunnelAutoRecoverySwitch, TunnelIpFamilyControl, TunnelOutboundSelect, TunnelProfileControl, TunnelProtocolControl,
} from '@/components/remoteAccess/TunnelControls';
import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '@/components/settings/SettingsPrimitives';
import { Badge } from '@/components/ui/badge';
import { useDraft } from './draft';
import { Para } from './devices-text';

type D = (zh: string, en: string) => string;
type Setting = 'protocol' | 'ipFamily' | 'outbound';
type Value = { zh: string; en: string };

/** The latest tunnel change of one setting, as `operation` / `outcome` report it. */
export type TunnelChange =
  | { setting: Setting; state: 'verifying' }
  | { setting: Setting; state: 'applied' }
  /** The change did not connect (or could not be checked) and the previous value is back. */
  | { setting: Setting; state: 'restored'; reason: 'verification_failed' | 'verification_unavailable'; from: Value }
  /** The previous value is saved but the tunnel is still coming back, or it could not be saved. */
  | { setting: Setting; state: 'failed'; reason: 'saved_restored_runtime_pending' | 'storage_restore_failed'; from: Value };

export type ConnectionV2Props = {
  /** Where the page is open: at the computer itself, or a browser / the App elsewhere. */
  viewer: 'local' | 'remote';
  direct: 'ready' | 'off';
  /** The relay row: absent unless this instance is enrolled from the host's command line. */
  relay?: { enabled: boolean; connected: boolean };
  change?: TunnelChange;
  /** A switch was refused, said in one line under it. */
  refused?: { row: 'direct' | 'relay'; reason: 'tunnel_unhealthy' | 'connection_change_busy' };
  /** Fixed values that could not connect for 3 minutes were set back to automatic. */
  returnedToAutomatic?: { at: Value; changes: Array<{ setting: Setting; from: Value }> };
  protocol: 'auto' | 'quic' | 'http2';
};

const settingName = (d: D): Record<Setting, string> => ({
  protocol: d('传输协议', 'Transport protocol'),
  ipFamily: d('边缘网络 IP', 'Edge IP family'),
  outbound: d('出口网卡', 'Outbound interface'),
});

function ChangeLine({ change }: { change: TunnelChange }) {
  const d = useDraft();
  const pick = (value: Value) => d(value.zh, value.en);
  const [Icon, tone, text] = change.state === 'verifying'
    ? [LoaderCircle, 'text-muted', d('验证中', 'Verifying')]
    : change.state === 'applied'
      ? [Check, 'text-mint-ink', d('已生效', 'In effect')]
      : change.state === 'restored'
        ? change.reason === 'verification_failed'
          ? [Undo2, 'text-gold-ink', d(`没连上，已改回「${pick(change.from)}」`, `Did not connect; set back to ${pick(change.from)}`)]
          : [Undo2, 'text-gold-ink', d(`无法验证（改之前隧道就不通），已改回「${pick(change.from)}」`, `Could not be checked (the tunnel was down before the change); set back to ${pick(change.from)}`)]
        : change.reason === 'saved_restored_runtime_pending'
          ? [TriangleAlert, 'text-gold-ink', d(`已改回「${pick(change.from)}」，隧道还在恢复`, `Set back to ${pick(change.from)}; the tunnel is still coming back`)]
          : [TriangleAlert, 'text-destructive-ink', d('没能改回原来的设置。请在运行 Avibe 的电脑上检查。', 'Could not set it back. Check it on the computer running Avibe.')];
  return (
    <div role="status" className={`mt-1 flex items-center gap-1.5 text-[11px] leading-relaxed ${tone}`}>
      <Icon className={`size-3.5 shrink-0 ${change.state === 'verifying' ? 'motion-safe:animate-spin' : ''}`} />
      <span><Para text={text} /></span>
    </div>
  );
}

function Row({ title, hint, status, control, tone }: { title: string; hint?: ReactNode; status?: ReactNode; control: ReactNode; tone?: 'info' }) {
  return (
    <div className={`flex flex-col gap-2 border-b border-border px-5 py-3 last:border-b-0 md:flex-row md:items-center md:justify-between md:gap-6 ${tone === 'info' ? 'bg-cyan/[0.06]' : ''}`}>
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-foreground">{title}</div>
        {hint && <div className="mt-0.5 text-[11px] leading-relaxed text-muted">{typeof hint === 'string' ? <Para text={hint} /> : hint}</div>}
        {status}
      </div>
      <div className="shrink-0">{control}</div>
    </div>
  );
}

function Refused({ reason }: { reason: 'tunnel_unhealthy' | 'connection_change_busy' }) {
  const d = useDraft();
  return (
    <div role="alert" className="mt-1 flex items-start gap-1.5 text-[11px] leading-relaxed text-gold-ink">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
      <span>
        <Para text={reason === 'tunnel_unhealthy'
          ? d('没有改：Cloudflare 隧道此刻不通，你正靠直连连着，改了会断开。', 'Not changed: the Cloudflare tunnel is down right now and you are connected through direct, so the change would cut you off.')
          : d('没有改：另一项更改还在进行，稍后再试。', 'Not changed: another change is still in progress. Try again shortly.')} />
      </span>
    </div>
  );
}

export function ConnectionV2({ viewer, direct, relay, change, refused, returnedToAutomatic, protocol }: ConnectionV2Props) {
  const d = useDraft();
  const pick = (value: Value) => d(value.zh, value.en);
  const statusFor = (setting: Setting) => (change?.setting === setting ? <ChangeLine change={change} /> : null);
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
          control={<Badge variant={viewer === 'remote' ? 'info' : 'success'}>{viewer === 'remote' ? d('使用中', 'In use') : d('可用', 'Available')}</Badge>}
          tone={viewer === 'remote' ? 'info' : undefined}
        />
        <Row
          title={d('直连', 'Direct')}
          hint={(
            <>
              <Para text={d('Avibe App 能直接连到这台电脑时就不绕路。关掉后都走 Cloudflare 隧道。', 'The Avibe App goes straight to this computer when it can. When off, everything uses the Cloudflare tunnel.')} />
              <br />
              <Para text={d(
                '注意：所在网络到不了 Cloudflare、正靠直连连着的手机，关掉直连后会断开，直到换到到得了的网络。',
                'Note: a phone connected through direct on a network that cannot reach Cloudflare is cut off when direct is turned off, until it moves to a network that can.',
              )} />
            </>
          )}
          status={refused?.row === 'direct' ? <Refused reason={refused.reason} /> : null}
          control={<ToggleSwitch enabled={direct !== 'off'} onClick={() => undefined} label={d('直连', 'Direct')} />}
        />
        {relay && (
          <Row
            title={d('中继', 'Relay')}
            hint={d(
              '打洞不通时经中继转发，通常比 Cloudflare 隧道慢。开着中继时，直连只有所有者能用。',
              'Forwards through a relay when hole punching fails; usually slower than the Cloudflare tunnel. While the relay is on, only the owner can use direct.',
            )}
            status={refused?.row === 'relay' ? <Refused reason={refused.reason} /> : relay.enabled ? (
              <div className="mt-1 text-[11px] text-muted">{relay.connected ? d('已连上中继', 'Connected to the relay') : d('还没连上中继', 'Not connected to the relay yet')}</div>
            ) : null}
            control={<ToggleSwitch enabled={relay.enabled} disabled={direct === 'off'} onClick={() => undefined} label={d('中继', 'Relay')} />}
          />
        )}
      </SettingsPanel>

      <SettingsPanel
        title={d('Cloudflare 隧道设置', 'Cloudflare tunnel settings')}
        description={d(
          '改了之后，Avibe 会自己验证隧道连不连得上；连不上就改回原来的设置。',
          'After a change, Avibe checks by itself that the tunnel connects; if it does not, the previous setting comes back.',
        )}
      >
        <Row
          title={d('传输协议', 'Transport protocol')}
          hint={protocol === 'auto' ? d('自动 · 当前 QUIC', 'Automatic · now QUIC') : d('固定后，网络变了不会自己换协议；连续 3 分钟连不上会改回自动。', 'Once fixed, it does not switch when the network changes; after 3 minutes without a connection it goes back to Automatic.')}
          status={statusFor('protocol')}
          control={<TunnelProtocolControl value={protocol} onChange={() => undefined} disabled={change?.state === 'verifying'} />}
        />
        <Row title={d('边缘网络 IP', 'Edge IP family')} status={statusFor('ipFamily')} control={<TunnelIpFamilyControl value="4" onChange={() => undefined} disabled={change?.state === 'verifying'} />} />
        <Row title={d('出口网卡', 'Outbound interface')} status={statusFor('outbound')} control={<TunnelOutboundSelect value="" onChange={() => undefined} interfaces={[]} className="md:w-56" disabled={change?.state === 'verifying'} />} />
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
    </SettingsPageShell>
  );
}
