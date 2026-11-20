// Proposed 2026-11-20 (frames only, a draft to discuss): Settings · Devices · 连接方式, the page
// behind the Devices page's 「连接方式」 row (ui/design: settings/devices-connection/*). Nothing here
// is needed by an ordinary user: everything defaults to automatic, and the Devices page shows only
// the path in use. Three groups, split by whose choice it is: what this Avibe offers (the
// Cloudflare tunnel, direct, the relay), the path this device uses (the Avibe App only), and the
// tunnel's own settings, moved here unchanged from today's Remote access page.
// A change made over a remote connection that could cut it is applied on trial: unless the same
// device comes back and keeps it, the instance puts the old value back by itself.
// Draft copy, inline in both languages. Path names are #4061's.
import type { ReactNode } from 'react';
import { ChevronRight, Route, Stethoscope } from 'lucide-react';

import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '@/components/settings/SettingsPrimitives';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { useDraft } from './draft';

type D = (zh: string, en: string) => string;

/** The instance's direct state, as `vibe remote direct status` reports it today. */
export type DirectState = 'ready' | 'off' | 'awaiting-permission' | 'denied' | 'awaiting-certificate' | 'not-running';
export type DevicePath =
  | { kind: 'tunnel'; relayUp?: boolean }
  | { kind: 'relay'; ms?: number }
  | { kind: 'lan' | 'punched'; family: 'IPv4' | 'IPv6'; ms?: number };
export type DeviceChoice = 'auto' | 'tunnel' | 'lan' | 'no-relay';

export type ConnectionPageProps = {
  /** Where the page is open: at the machine itself, a browser elsewhere, or the Avibe App. */
  viewer: 'local' | 'browser' | 'app';
  tunnel: { ms: number; protocol: 'auto' | 'quic' | 'http2'; active: 'QUIC' | 'HTTP/2'; ip: 'auto' | '4' | '6'; autoRecovery: boolean; profile: 'stable' | 'balanced' | 'low_latency' };
  direct: DirectState;
  relay: boolean;
  /** The Avibe App's own path, as it reports it. */
  path?: DevicePath;
  choice?: DeviceChoice;
  /** A change is on trial: the same device came back and may keep it. */
  trial?: { what: { zh: string; en: string }; back: { zh: string; en: string }; seconds: number };
  /** A trial ended without this device coming back: the old value is in place again. */
  reverted?: { what: { zh: string; en: string }; back: { zh: string; en: string } };
  /** This Avibe's domain, for the owner: the last row of the page, and the way to change it. */
  host?: string;
  /** Changing the domain (owner, 2026-11-20: a plain rename, the consequences are the user's):
   *  `form` says what will happen and takes the new name, `confirm` asks a second time,
   *  `done` is this page afterwards, at the computer itself. */
  rename?: { step: 'form' | 'confirm' | 'done'; name: string; taken?: boolean; from?: string; holdDays: number };
};

const directWord = (d: D): Record<DirectState, string> => ({
  ready: d('可用', 'Available'),
  off: d('未开启', 'Off'),
  'awaiting-permission': d('等待授权', 'Awaiting permission'),
  denied: d('未获准', 'Not allowed'),
  'awaiting-certificate': d('等待证书', 'Awaiting certificate'),
  'not-running': d('未运行', 'Not running'),
});

const directWhy = (d: D): Partial<Record<DirectState, string>> => ({
  'awaiting-permission': d('正在向 avibe.bot 申请为这个 Avibe 开放直连。', 'Asking avibe.bot to allow direct for this Avibe.'),
  denied: d('直连还没有对这个 Avibe 开放。', 'Direct is not yet available for this Avibe.'),
  'awaiting-certificate': d('正在为这个域名申请证书，好了会自动开始。', 'Getting a certificate for this domain; it starts by itself when ready.'),
  'not-running': d('直连组件没有运行。重启 Avibe 后再看。', 'The direct component is not running. Restart Avibe and look again.'),
});

function PathRow({ name, detail, state, why, ms, control }: {
  name: string; detail: string; state: { label: string; variant: 'info' | 'success' | 'secondary' | 'warning' }; why?: string; ms?: number; control?: ReactNode;
}) {
  return (
    <div className={`flex items-center gap-3 border-b border-border px-5 py-3 last:border-b-0 ${state.variant === 'info' ? 'bg-cyan/[0.06]' : ''}`}>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-foreground">{name}</div>
        <div className={`mt-0.5 text-[11px] leading-relaxed ${why ? 'text-gold-ink' : 'text-muted'}`}>{why ?? detail}</div>
      </div>
      {ms !== undefined && <span className="shrink-0 text-[11px] tabular-nums text-muted">{ms} ms</span>}
      <Badge variant={state.variant} className="shrink-0">{state.label}</Badge>
      {control && <div className="shrink-0">{control}</div>}
    </div>
  );
}

function Setting({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2 border-b border-border px-5 py-3 last:border-b-0 md:flex-row md:items-center md:justify-between md:gap-6">
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-foreground">{title}</div>
        {hint && <div className="mt-0.5 text-[11px] leading-relaxed text-muted">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

export function ConnectionPage({ viewer, tunnel, direct, relay, path, choice = 'auto', trial, reverted, host, rename }: ConnectionPageProps) {
  const d = useDraft();
  const pick = (text: { zh: string; en: string }) => d(text.zh, text.en);
  const remote = viewer !== 'local';
  const inUse = { label: d('使用中', 'In use'), variant: 'info' as const };
  const available = { label: d('可用', 'Available'), variant: 'success' as const };
  const directState = direct === 'ready' ? available
    : { label: directWord(d)[direct], variant: direct === 'off' ? 'secondary' as const : 'warning' as const };
  const why = directWhy(d)[direct];
  const relayState = !relay ? { label: d('未开启', 'Off'), variant: 'secondary' as const } : direct === 'ready' ? available : directState;
  const using = (kind: DevicePath['kind']) => path?.kind === kind;
  const pathLine = !path ? null
    : path.kind === 'tunnel' ? d('Cloudflare 隧道', 'Cloudflare tunnel') + (path.relayUp ? d('（中继可用，但更慢）', ' (relay available, but slower)') : '')
      : path.kind === 'relay' ? d('中继', 'Relay') + (path.ms ? ` · ${path.ms} ms` : '')
        : `${path.kind === 'lan' ? d('局域网直连', 'Direct (local network)') : d('打洞直连', 'Direct (hole punching)')} · ${path.family}${path.ms ? ` · ${path.ms} ms` : ''}`;
  const trialNote = d(
    '从远处更改会先试用：这台设备 30 秒内没有连回来确认，就自动改回去。',
    'A change made remotely is on trial: unless this device comes back and keeps it within 30 seconds, it is put back by itself.',
  );

  return (
    <SettingsPageShell
      title={d('连接方式', 'Connection')}
      subtitle={d('Avibe 会自动走最快的那条路，断了自动换。这里一般不用动。', 'Avibe uses the fastest path by itself and switches when one fails. You rarely need to change anything here.')}
      activeTab="remote-access"
      breadcrumb={<span>{d('设备 / 连接方式', 'Devices / Connection')}</span>}
    >
      {rename?.step === 'done' && (
        <div className="rounded-xl border border-mint/40 bg-mint-soft px-5 py-3 text-[12px] leading-relaxed text-foreground">
          {d(`域名已改为 ${host}。所有设备都已退出，要在新地址重新登录；${rename.from} 已作废。`,
            `The domain is now ${host}. Every device was signed out and signs in again at the new address; ${rename.from} is void.`)}
        </div>
      )}
      {reverted && (
        <div className="rounded-xl border border-gold/40 bg-gold/10 px-5 py-3 text-[12px] leading-relaxed text-foreground">
          {d(`把${pick(reverted.what)}之后，这台设备没有连回来，已自动改回${pick(reverted.back)}。`,
            `After ${pick(reverted.what)}, this device did not come back, so it was put back to ${pick(reverted.back)} by itself.`)}
        </div>
      )}

      {viewer === 'app' && (
        <SettingsPanel
          title={d('这台设备', 'This device')}
          description={pathLine ? d(`正在使用：${pathLine}`, `Using: ${pathLine}`) : d('还没有报告路径', 'No path reported yet')}
        >
          <div className="flex flex-col">
            {([
              ['auto', d('自动', 'Automatic'), d('哪条快走哪条，断了自动换。', 'The fastest path, switching when one fails.')],
              ['tunnel', d('仅 Cloudflare 隧道', 'Cloudflare tunnel only'), d('不尝试直连和中继。', 'Never tries direct or the relay.')],
              ['no-relay', d('不走中继', 'No relay'), d('直连打不通时，直接走 Cloudflare 隧道。', 'When direct fails, goes straight to the Cloudflare tunnel.')],
              ['lan', d('仅局域网直连', 'Local network only'), d('只有和这台电脑在同一网络时才能打开。', 'Opens only on the same network as this computer.')],
            ] as const).map(([id, label, text]) => (
              <div key={id} className="flex items-center gap-3 border-b border-border px-5 py-2.5 last:border-b-0" role="radio" aria-checked={choice === id}>
                <span className={`flex size-4 shrink-0 items-center justify-center rounded-full border ${choice === id ? 'border-mint bg-mint' : 'border-border-strong'}`}>
                  {choice === id && <span className="size-1.5 rounded-full bg-background" />}
                </span>
                <div className="min-w-0">
                  <div className="text-[13px] font-medium text-foreground">{label}</div>
                  <div className="text-[11px] leading-relaxed text-muted">{text}</div>
                </div>
              </div>
            ))}
          </div>
          <p className="border-t border-border px-5 py-2.5 text-[11px] leading-relaxed text-muted">
            {d('这个选择只管这台设备。选了之后连不上，App 会自己改回「自动」。', 'This choice is for this device only. If it then cannot connect, the App goes back to Automatic by itself.')}
          </p>
        </SettingsPanel>
      )}

      <SettingsPanel
        title={d('这个 Avibe 提供的路径', 'Paths this Avibe offers')}
        description={viewer === 'app' ? undefined : d('浏览器只走 Cloudflare 隧道；直连和中继由 Avibe App 使用。', 'Browsers use the Cloudflare tunnel only; direct and the relay are used by the Avibe App.')}
      >
        <PathRow
          name={d('Cloudflare 隧道', 'Cloudflare tunnel')}
          detail={d('所有设备都能走，也是其他路径的后备。不能关闭。', 'Every device can use it, and it backs up the other paths. It cannot be turned off.')}
          state={using('tunnel') || viewer !== 'app' ? inUse : available}
          ms={tunnel.ms}
        />
        <div className="flex items-center justify-between gap-4 border-b border-border px-5 py-3">
          <div className="min-w-0">
            <div className="text-[13px] font-medium text-foreground">{d('直连', 'Direct')}</div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
              {d('Avibe App 能直接连到这台电脑时就不绕路。关掉后都走 Cloudflare 隧道。', 'The Avibe App goes straight to this computer when it can. When off, everything uses the Cloudflare tunnel.')}
            </div>
          </div>
          <ToggleSwitch enabled={direct !== 'off'} onClick={() => undefined} label={d('直连', 'Direct')} />
        </div>
        <PathRow
          name={d('局域网直连', 'Direct (local network)')}
          detail={d('和这台电脑在同一网络时。', 'On the same network as this computer.')}
          state={using('lan') ? inUse : directState}
          why={why}
          ms={path?.kind === 'lan' ? path.ms : undefined}
        />
        <PathRow
          name={d('打洞直连', 'Direct (hole punching)')}
          detail={d('不在同一网络时，两边直接相连。', 'On different networks, the two sides connect directly.')}
          state={using('punched') ? inUse : directState}
          why={why}
          ms={path?.kind === 'punched' ? path.ms : undefined}
        />
        <PathRow
          name={d('中继', 'Relay')}
          detail={d('打洞不通时经中继转发，通常比 Cloudflare 隧道慢。', 'Forwards through a relay when hole punching fails; usually slower than the Cloudflare tunnel.')}
          state={using('relay') ? inUse : relayState}
          why={relay ? why : undefined}
          ms={path?.kind === 'relay' ? path.ms : undefined}
          control={<ToggleSwitch enabled={relay} disabled={direct === 'off'} onClick={() => undefined} label={d('中继', 'Relay')} />}
        />
      </SettingsPanel>

      <SettingsPanel
        title={d('Cloudflare 隧道设置', 'Cloudflare tunnel settings')}
        description={remote ? trialNote : d('在这台电脑上更改，立即生效。', 'Changed at this computer, they apply at once.')}
      >
        <Setting
          title={d('传输协议', 'Transport protocol')}
          hint={tunnel.protocol === 'auto'
            ? d(`自动 · 当前 ${tunnel.active}`, `Automatic · now ${tunnel.active}`)
            : d('固定后，网络变了不会自己换协议；连续 3 分钟连不上会改回自动。', 'Once fixed, it does not switch when the network changes; after 3 minutes without a connection it goes back to Automatic.')}
        >
          <SegmentedRadio
            ariaLabel={d('传输协议', 'Transport protocol')}
            value={tunnel.protocol}
            onChange={() => undefined}
            options={[{ id: 'auto', label: d('自动', 'Automatic') }, { id: 'quic', label: 'QUIC' }, { id: 'http2', label: 'HTTP/2' }]}
          />
        </Setting>
        <Setting title={d('边缘网络 IP', 'Edge network IP')}>
          <SegmentedRadio
            ariaLabel={d('边缘网络 IP', 'Edge network IP')}
            value={tunnel.ip}
            onChange={() => undefined}
            options={[{ id: 'auto', label: d('自动', 'Automatic') }, { id: '4', label: 'IPv4' }, { id: '6', label: 'IPv6' }]}
          />
        </Setting>
        <Setting title={d('出口网卡', 'Outbound interface')}>
          <Button variant="secondary" size="xs">{d('跟随系统', 'System default')}<ChevronRight className="rotate-90" /></Button>
        </Setting>
        <Setting title={d('自动优化线路', 'Optimize the route automatically')}>
          <ToggleSwitch enabled={tunnel.autoRecovery} onClick={() => undefined} label={d('自动优化线路', 'Optimize the route automatically')} />
        </Setting>
        <Setting title={d('优化策略', 'Optimization profile')}>
          <SegmentedRadio
            ariaLabel={d('优化策略', 'Optimization profile')}
            value={tunnel.profile}
            onChange={() => undefined}
            options={[{ id: 'stable', label: d('稳定优先', 'Stable') }, { id: 'balanced', label: d('均衡', 'Balanced') }, { id: 'low_latency', label: d('低延迟', 'Low latency') }]}
          />
        </Setting>
      </SettingsPanel>

      <SettingsPanel>
        <div className="flex items-center gap-3 border-b border-border px-5 py-3 text-[13px] text-foreground">
          <Route className="size-4 shrink-0 text-muted" />
          <span className="flex-1">{d('优化线路', 'Optimize the route')}</span>
          <ChevronRight className="size-4 text-muted" />
        </div>
        <div className="flex items-center gap-3 px-5 py-3 text-[13px] text-foreground">
          <Stethoscope className="size-4 shrink-0 text-muted" />
          <span className="flex-1">{d('连接诊断与网络路径', 'Diagnostics and network path')}</span>
          <ChevronRight className="size-4 text-muted" />
        </div>
      </SettingsPanel>

      {host && viewer !== 'app' && (
        <SettingsPanel>
          <div className="flex items-center gap-3 px-5 py-3">
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-foreground">{d('域名', 'Domain')}</div>
              <div className="mt-0.5 truncate font-mono text-[11px] text-muted">{host}</div>
            </div>
            <Button variant="ghost" size="compact" className="text-destructive-ink">{d('更改域名', 'Change the domain')}</Button>
          </div>
        </SettingsPanel>
      )}

      {rename?.step === 'form' && host && (
        <Dialog open onOpenChange={() => undefined}>
          <DialogContent className="max-w-[480px]" closeLabel={d('关闭', 'Close')}>
            <DialogHeader>
              <DialogTitle>{d('更改域名', 'Change the domain')}</DialogTitle>
              <DialogDescription>{d(`现在是 ${host}。`, `It is ${host} now.`)}</DialogDescription>
            </DialogHeader>
            <div className="flex flex-col gap-1.5">
              <div className="flex min-w-0 items-center gap-2">
                <Input className="w-48 text-[13px]" value={rename.name} readOnly aria-label={d('新名称', 'New name')} aria-invalid={rename.taken} />
                <span className="truncate font-mono text-[12px] text-muted">-app.avibe.bot</span>
              </div>
              {rename.taken && <span className="text-[11px] text-destructive-ink">{d('已被占用，换一个名称。', 'Taken. Choose another name.')}</span>}
            </div>
            <div className="rounded-lg bg-field px-4 py-3">
              <div className="text-[12px] font-semibold text-foreground">{d('更改后', 'After the change')}</div>
              <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-[12px] leading-relaxed text-muted">
                <li>{d(`${host} 立即作废，${rename.holdDays} 天后别人可以领取，不会还给你。`, `${host} is void at once; after ${rename.holdDays} days someone else may claim it. You do not get it back.`)}</li>
                <li>{d('所有设备都会退出，要在新地址重新登录。', 'Every device is signed out and signs in again at the new address.')}</li>
                <li>{d('已经发出去的链接（展示页、文件）全部失效。', 'Every link already sent (Show Pages, files) stops working.')}</li>
                <li>{d('主屏幕 App 要删掉重新添加；手机通知和健康要重新连接。', 'A home-screen app must be removed and added again; phone notifications and Health must be connected again.')}</li>
                <li>{d('直连要等新证书，这期间走 Cloudflare 隧道。', 'Direct waits for a new certificate; until then everything uses the Cloudflare tunnel.')}</li>
              </ul>
            </div>
            <DialogFooter>
              <Button variant="ghost">{d('取消', 'Cancel')}</Button>
              <Button variant="default" disabled={rename.taken}>{d('继续', 'Continue')}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {rename?.step === 'confirm' && host && (
        <Dialog open onOpenChange={() => undefined}>
          <DialogContent className="max-w-md" closeLabel={d('关闭', 'Close')}>
            <DialogHeader>
              <DialogTitle>{d(`改成 ${rename.name}-app.avibe.bot？`, `Change to ${rename.name}-app.avibe.bot?`)}</DialogTitle>
              <DialogDescription>
                {d(`这不能撤销。${host} 立即作废，所有设备都会退出，包括你现在用的这一台。`,
                  `This cannot be undone. ${host} is void at once and every device is signed out, this one included.`)}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost">{d('取消', 'Cancel')}</Button>
              <Button variant="destructive" disabled>{d('更改域名（3）', 'Change the domain (3)')}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {trial && (
        <Dialog open onOpenChange={() => undefined}>
          <DialogContent className="max-w-md" closeLabel={d('关闭', 'Close')}>
            <DialogHeader>
              <DialogTitle>{d('保留这个更改？', 'Keep this change?')}</DialogTitle>
              <DialogDescription>
                {d(`${pick(trial.what)}，这台设备连回来了。${trial.seconds} 秒内不保留，会自动改回${pick(trial.back)}。`,
                  `${pick(trial.what)}, and this device came back. Unless you keep it within ${trial.seconds} seconds, it goes back to ${pick(trial.back)} by itself.`)}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost">{d('改回去', 'Put it back')}</Button>
              <Button variant="default">{d(`保留（${trial.seconds}）`, `Keep (${trial.seconds})`)}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </SettingsPageShell>
  );
}
