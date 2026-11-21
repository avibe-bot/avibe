// Proposed 2026-11-21 (frames only): the Devices page with the states today's backend adds
// (ui/design: settings/devices-functions/*). A function the viewer may change is a switch; one it
// may not is plain text (never a switch that is refused when pressed). A Health connection no device
// can be proven to own is its own row, disconnectable on its own. Signing a device out ends it here
// at once and asks avibe.bot to end the account sign-in; the page says what avibe.bot actually did,
// never 「已结束」 for a request merely accepted. Contracts: docs/plans/device-functions-activity.md,
// device-account-session-sign-out.md (#4107). Draft copy, inline in both languages.
import { AlarmClock, Bell, Check, Clock, ExternalLink, Copy, HeartPulse, LoaderCircle, LogOut, MonitorCog, Smartphone, TriangleAlert, X } from 'lucide-react';

import { SettingsPanel, ToggleSwitch } from '@/components/settings/SettingsPrimitives';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useDraft } from './draft';
import { Para } from './devices-text';

type D = (zh: string, en: string) => string;

const addressOf = (host: string) => `https://${host}`;
type Text = { zh: string; en: string };

export type Fn =
  | { kind: 'notifications' | 'schedule'; on: boolean; canChange: boolean }
  | { kind: 'health'; on: boolean; canChange: boolean; detail: Text }
  /** `here`: the page is open on that computer itself. */
  | { kind: 'computerUse'; status: 'ready' | 'needs_permission' | 'off' | 'unavailable'; here?: boolean };

const fnName = (d: D, kind: Fn['kind']) => ({
  notifications: d('通知', 'Notifications'),
  schedule: d('闹钟与提醒', 'Alarms and reminders'),
  health: d('健康', 'Health'),
  computerUse: d('电脑操作', 'Computer Use'),
})[kind];
const fnIcon = { notifications: Bell, schedule: AlarmClock, health: HeartPulse, computerUse: MonitorCog } as const;

/** One function: a switch when this viewer may change it, its state in words when not. */
function FunctionRow({ fn }: { fn: Fn }) {
  const d = useDraft();
  const Icon = fnIcon[fn.kind];
  const name = fnName(d, fn.kind);
  const state = fn.kind === 'computerUse'
    ? { ready: d('已就绪', 'Ready'), needs_permission: fn.here ? d('需要授权', 'Needs permission') : d('需要在那台电脑上授权', 'Needs permission on that computer'), off: d('未开启', 'Off'), unavailable: d('暂时不可用', 'Unavailable right now') }[fn.status]
    : fn.on ? d('已开启', 'On') : d('未开启', 'Off');
  const detail = fn.kind === 'health' ? d(fn.detail.zh, fn.detail.en)
    : fn.kind === 'computerUse' ? fn.here ? d('在「电脑操作」设置里开关。', 'Turned on or off in the Computer Use settings.') : d('在那台电脑的 Avibe 里开关。', 'Turned on or off in Avibe on that computer.')
      : fn.kind === 'notifications' ? d('伙伴和会话的消息会推送到这台设备。', 'Messages from Bots and Sessions are pushed to this device.')
        : d('由 Agent 提出，每次都由你在这台设备上确认。', 'An Agent asks; you confirm each one on this device.');
  const canChange = fn.kind !== 'computerUse' && fn.canChange && !(fn.kind === 'health' && !fn.on);
  return (
    <div className="flex items-center gap-3 px-6 py-2.5 max-md:px-5">
      <Icon className="size-4 shrink-0 text-muted" />
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-foreground">{name}</div>
        <div className="text-[11px] leading-relaxed text-muted"><Para text={detail} /></div>
      </div>
      {canChange
        ? <ToggleSwitch enabled={'on' in fn && fn.on} onClick={() => undefined} label={name} />
        : <span className="shrink-0 text-right text-[12px] text-muted">{state}</span>}
    </div>
  );
}

/** A device's functions as this viewer sees them. */
export function FunctionsDialog({ name, app, functions, note }: { name: string; app: string; functions: Fn[]; note?: Text }) {
  const d = useDraft();
  return (
    <Dialog open onOpenChange={() => undefined}>
      <DialogContent className="max-w-md gap-0 p-0 max-md:p-0" closeLabel={d('关闭', 'Close')}>
        <DialogHeader className="px-6 pb-4 pt-6 max-md:px-5 max-md:pt-3">
          <div className="flex items-center gap-3.5">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-field text-muted"><Smartphone size={18} /></span>
            <div className="min-w-0 text-left">
              <DialogTitle className="truncate text-[15px]">{name}</DialogTitle>
              <DialogDescription className="mt-0.5 truncate text-[12px]">{app}</DialogDescription>
            </div>
          </div>
        </DialogHeader>
        <div className="border-t border-border pb-2">
          <div className="px-6 pb-1 pt-3.5 text-[11px] font-semibold text-muted max-md:px-5">{d('功能', 'Functions')}</div>
          {functions.map((fn) => <FunctionRow key={fn.kind} fn={fn} />)}
          {note && <p className="px-6 pb-2 pt-1 text-[11px] leading-relaxed text-muted max-md:px-5"><Para text={d(note.zh, note.en)} /></p>}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Health connections no listed device can be proven to own: one row each, disconnectable on its own. */
export function UnlinkedHealthPanel({ items }: { items: Array<{ device: string; data: Text; lastSeen: Text }> }) {
  const d = useDraft();
  return (
    <SettingsPanel
      title={d('未对应到设备的健康连接', 'Health connections not matched to a device')}
      description={<Para text={d(
        '这些 iPhone 连着健康，但认不出是列表里的哪一台。可以在这里单独断开；在 iPhone 上重新打开 Avibe App 后通常会自己对上。',
        'These iPhones have Health connected, but cannot be matched to a device in the list. You can disconnect each here; opening the Avibe App on the iPhone again usually matches it by itself.',
      )} />}
    >
      {items.map((item) => (
        <div key={item.device} className="flex items-center gap-3.5 border-b border-border px-5 py-3 last:border-b-0">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-field text-muted"><HeartPulse size={18} /></span>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium text-foreground">{item.device}</div>
            <div className="text-[11px] leading-relaxed text-muted">{d(`${item.data.zh} · 上次连接 ${item.lastSeen.zh}`, `${item.data.en} · last connected ${item.lastSeen.en}`)}</div>
          </div>
          <Button variant="destructive-soft" size="sm">{d('断开', 'Disconnect')}</Button>
        </div>
      ))}
    </SettingsPanel>
  );
}

/** What avibe.bot did about the account sign-in: `requesting` (queued, every sign-out starts
 *  here) and `retrying` are the two halves of the contract's `pending`. */
export type SignOutAccount = 'requesting' | 'retrying' | 'accepted' | 'not_applicable' | 'unconfirmed' | 'refused';
export type SignOutRow = { device: string; at: Text; account: SignOutAccount };

const accountLine = (d: D, status: SignOutAccount) => ({
  requesting: d('正在请求 avibe.bot 结束这台设备的账号登录。', 'Asking avibe.bot to end this device’s account sign-in.'),
  retrying: d('暂时连不上 avibe.bot，会自动重试，连上后结束它的账号登录。', 'Cannot reach avibe.bot right now; Avibe keeps trying and ends its account sign-in once it gets through.'),
  accepted: d('已通知 avibe.bot 结束它的账号登录。', 'Told avibe.bot to end its account sign-in.'),
  not_applicable: d('这次没有结束它的账号登录，它可能不用验证就能再登录。设备丢了的话，到 avibe.bot 的「账号安全」里退出其他设备。', 'Its account sign-in was not ended this time, and it may be able to sign in again without verifying. If the device is lost, go to Account security at avibe.bot and choose Sign out other devices.'),
  unconfirmed: d('多次重试后，仍不确定它的账号登录有没有结束。设备丢了的话，到 avibe.bot 的「账号安全」里退出其他设备。', 'After many tries, it is still unclear whether its account sign-in ended. If the device is lost, go to Account security at avibe.bot and choose Sign out other devices.'),
  refused: d('avibe.bot 没有接受这次请求，这次没有结束它的账号登录。设备丢了的话，到 avibe.bot 的「账号安全」里退出其他设备。', 'avibe.bot did not accept the request, so its account sign-in was not ended this time. If the device is lost, go to Account security at avibe.bot and choose Sign out other devices.'),
})[status];
const accountTone = (status: SignOutAccount) => (status === 'accepted' ? 'text-mint-ink' : status === 'refused' || status === 'unconfirmed' ? 'text-gold-ink' : 'text-muted');
const accountIcon = { requesting: LoaderCircle, retrying: LoaderCircle, accepted: Check, not_applicable: LogOut, unconfirmed: Clock, refused: X } as const;

/** Devices signed out lately, each with what avibe.bot did about its account sign-in. */
export function RecentSignOutsPanel({ rows }: { rows: SignOutRow[] }) {
  const d = useDraft();
  return (
    <SettingsPanel title={d('最近退出的设备', 'Recently signed out')}>
      {rows.map((row) => {
        const Icon = accountIcon[row.account];
        return (
          <div key={row.device} className="flex items-start gap-3 border-b border-border px-5 py-3 last:border-b-0">
            <Icon className={`mt-0.5 size-4 shrink-0 ${accountTone(row.account)}`} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-[13px] font-medium text-foreground">{row.device}</span>
                <span className="text-[11px] text-muted">{d(`已在这个 Avibe 上退出 · ${row.at.zh}`, `Signed out of this Avibe · ${row.at.en}`)}</span>
              </div>
              <div className="mt-0.5 text-[12px] leading-relaxed text-foreground"><Para text={accountLine(d, row.account)} /></div>
            </div>
          </div>
        );
      })}
    </SettingsPanel>
  );
}

/** Right after signing a device out (the response): what avibe.bot was asked or not asked, and Health
 *  only here, because only this response says anything about it (`health_cleanup_pending`). A pairing
 *  that stays connected shows later under Health connections not matched to a device. */
export function SignOutNotice({ device, account, health }: { device: string; account: SignOutAccount; health?: 'cleanup_pending' }) {
  const d = useDraft();
  const Icon = accountIcon[account];
  return (
    <div role="status" className="flex gap-3 rounded-xl border border-edge bg-surface px-5 py-3 shadow-elevation-0">
      <Icon className={`mt-0.5 size-4 shrink-0 ${accountTone(account)}`} />
      <div className="min-w-0 text-[12px] leading-relaxed text-foreground">
        <div className="font-medium">{d(`已在这个 Avibe 上退出 ${device}`, `${device} is signed out of this Avibe`)}</div>
        <div><Para text={accountLine(d, account)} /></div>
        {health === 'cleanup_pending' && <div className="text-muted"><Para text={d('它同步过的健康数据，清理尚未确认。', 'Clearing the Health data it synced is not confirmed yet.')} /></div>}
      </div>
    </div>
  );
}

/** A device that was signed out comes back while avibe.bot is still ending its sign-in. */
export function SignInNotFinishedPage() {
  const d = useDraft();
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-md">
        <div className="mb-6 grid size-12 place-items-center rounded-2xl border border-edge bg-field"><TriangleAlert className="size-5 text-gold-ink" /></div>
        <h1 className="text-xl font-semibold text-foreground">{d('这台设备已被退出', 'This device was signed out')}</h1>
        <p className="mt-3 text-[14px] leading-relaxed text-muted">
          <Para text={d(
            '请过几分钟再登录。一直不行的话，先在 avibe.bot 退出登录，再回来登录。',
            'Sign in again in a few minutes. If it keeps failing, sign out at avibe.bot first, then come back and sign in.',
          )} />
        </p>
        <div className="mt-7 flex flex-wrap gap-3">
          <Button variant="brand">{d('重新登录', 'Sign in again')}</Button>
          <Button variant="ghost"><ExternalLink />{d('打开 avibe.bot', 'Open avibe.bot')}</Button>
        </div>
      </div>
    </main>
  );
}

/** Remote access was claimed from another computer on the network: this browser must move to the new address. */
export function OpenNewAddressCard({ host }: { host: string }) {
  const d = useDraft();
  const url = addressOf(host);
  return (
    <SettingsPanel>
      <div className="flex flex-col gap-3 px-5 py-4">
        <div className="flex items-center gap-2">
          <Badge variant="success"><Check className="size-3" />{d('已开启', 'On')}</Badge>
          <span className="text-[14px] font-semibold text-foreground">{d('远程访问已开启', 'Remote access is on')}</span>
        </div>
        <p className="text-[13px] leading-relaxed text-foreground">{d('从现在起，请用新地址打开这个 Avibe：', 'From now on, open this Avibe at its new address:')}</p>
        <div className="flex flex-wrap items-center gap-2">
          <a href={url} className="font-mono text-[14px] font-semibold text-cyan-ink hover:underline">{url}</a>
          <Button variant="ghost" size="2xs" aria-label={d('复制地址', 'Copy address')}><Copy /></Button>
        </div>
        <div><Button variant="brand"><ExternalLink />{d('打开新地址', 'Open the new address')}</Button></div>
        <p className="text-[11px] leading-relaxed text-muted">
          <Para text={d(
            '这个页面是从局域网地址打开的，绑定后需要登录，所以不再更新。',
            'This page was opened at a local network address; after binding it needs a sign-in, so it no longer updates.',
          )} />
        </p>
      </div>
    </SettingsPanel>
  );
}
