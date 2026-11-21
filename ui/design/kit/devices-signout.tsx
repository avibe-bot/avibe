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
type Text = { zh: string; en: string };

export type Fn =
  | { kind: 'notifications' | 'schedule'; on: boolean; canChange: boolean }
  | { kind: 'health'; on: boolean; canChange: boolean; detail: Text }
  | { kind: 'computerUse'; status: 'ready' | 'needs_permission' | 'off' | 'unavailable' };

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
    ? { ready: d('已就绪', 'Ready'), needs_permission: d('需要在那台电脑上授权', 'Needs permission on that computer'), off: d('未开启', 'Off'), unavailable: d('暂时不可用', 'Unavailable right now') }[fn.status]
    : fn.on ? d('已开启', 'On') : d('未开启', 'Off');
  const detail = fn.kind === 'health' ? d(fn.detail.zh, fn.detail.en)
    : fn.kind === 'computerUse' ? d('在那台电脑的 Avibe 里开关。', 'Turned on or off in Avibe on that computer.')
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
      <DialogContent className="max-w-[480px] gap-0 p-0 max-md:p-0" closeLabel={d('关闭', 'Close')}>
        <DialogHeader className="px-6 pb-4 pt-6 max-md:px-5 max-md:pt-3">
          <div className="flex items-center gap-3.5">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-field text-muted"><Smartphone size={18} /></span>
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

/** A Health connection no listed device can be proven to own: one row, disconnectable on its own. */
export function UnlinkedHealthPanel({ device, synced }: { device: string; synced: Text }) {
  const d = useDraft();
  return (
    <SettingsPanel
      title={d('未对应到设备的健康连接', 'Health connection not matched to a device')}
      description={d(
        '这台 iPhone 连着健康，但认不出它是列表里的哪一台。可以在这里单独断开；在 iPhone 上重新打开 Avibe App 后通常会自己对上。',
        'This iPhone has Health connected, but it cannot be matched to a device in the list. You can disconnect it here; opening the Avibe App on the iPhone again usually matches it by itself.',
      )}
    >
      <div className="flex items-center gap-3.5 px-5 py-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-field text-muted"><HeartPulse size={18} /></span>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium text-foreground">{device}</div>
          <div className="text-[11px] leading-relaxed text-muted">{d(`iOS · 上次同步 ${synced.zh}`, `iOS · last synced ${synced.en}`)}</div>
        </div>
        <Button variant="destructive-soft" size="sm">{d('断开', 'Disconnect')}</Button>
      </div>
    </SettingsPanel>
  );
}

export type SignOutAccount = 'accepted' | 'pending' | 'not_applicable' | 'unconfirmed' | 'refused';
export type SignOutRow = {
  device: string;
  at: Text;
  account: SignOutAccount;
  /** Signed out at the computer itself: whether this row showed Health, and what the response said. */
  health?: 'cleanup_pending' | 'skipped_unverified';
};

const accountLine = (d: D, status: SignOutAccount) => ({
  accepted: d('已请求 avibe.bot 结束这台设备的账号登录。', 'Asked avibe.bot to end this device’s account sign-in.'),
  pending: d('待处理：暂时连不上 avibe.bot，会继续重试。', 'Pending: avibe.bot cannot be reached right now; Avibe keeps trying.'),
  not_applicable: d('只在这个 Avibe 上退出了；avibe.bot 上的账号登录不受影响。', 'Signed out of this Avibe only; the account sign-in at avibe.bot is unaffected.'),
  unconfirmed: d('未确认：多次重试后，仍不知道 avibe.bot 是否结束了这台设备的账号登录。', 'Not confirmed: after many tries it is still unknown whether avibe.bot ended this device’s account sign-in.'),
  refused: d('avibe.bot 没有接受结束账号登录的请求；这台设备只在这个 Avibe 上退出了。', 'avibe.bot did not accept the request to end the account sign-in; this device is signed out of this Avibe only.'),
})[status];
const accountIcon = { accepted: Check, pending: LoaderCircle, not_applicable: LogOut, unconfirmed: Clock, refused: X } as const;

/** Devices signed out lately, each with what avibe.bot did about its account sign-in. */
export function RecentSignOutsPanel({ rows }: { rows: SignOutRow[] }) {
  const d = useDraft();
  return (
    <SettingsPanel title={d('最近退出的设备', 'Recently signed out')}>
      {rows.map((row) => {
        const Icon = accountIcon[row.account];
        return (
          <div key={row.device} className="flex items-start gap-3 border-b border-border px-5 py-3 last:border-b-0">
            <Icon className={`mt-0.5 size-4 shrink-0 ${row.account === 'accepted' ? 'text-mint-ink' : row.account === 'refused' || row.account === 'unconfirmed' ? 'text-gold-ink' : 'text-muted'}`} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-[13px] font-medium text-foreground">{row.device}</span>
                <span className="text-[11px] text-muted">{d(`已在这个 Avibe 上退出 · ${row.at.zh}`, `Signed out of this Avibe · ${row.at.en}`)}</span>
              </div>
              <div className="mt-0.5 text-[12px] leading-relaxed text-foreground"><Para text={accountLine(d, row.account)} /></div>
              {row.health === 'cleanup_pending' && (
                <div className="mt-0.5 text-[11px] leading-relaxed text-muted"><Para text={d('它同步过的健康数据，清理尚未确认。', 'Clearing the Health data it synced is not confirmed yet.')} /></div>
              )}
              {row.health === 'skipped_unverified' && (
                <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
                  <Para text={d(
                    '它的健康连接没有断开：没能确认是所有者在这台电脑前操作。可以在上面的「未对应到设备的健康连接」里单独断开。',
                    'Its Health connection was not disconnected: it could not be confirmed that the owner is at this computer. You can disconnect it on its own under Health connection not matched to a device.',
                  )} />
                </div>
              )}
            </div>
          </div>
        );
      })}
    </SettingsPanel>
  );
}

/** A device that was signed out comes back while avibe.bot is still ending its sign-in. */
export function SignInNotFinishedPage() {
  const d = useDraft();
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-md">
        <div className="mb-6 grid size-12 place-items-center rounded-2xl border border-edge bg-field"><TriangleAlert className="size-5 text-gold-ink" /></div>
        <h1 className="text-xl font-semibold text-foreground">{d('登录未完成', 'Sign-in not finished')}</h1>
        <p className="mt-3 text-[14px] leading-relaxed text-muted">
          <Para text={d(
            '这台设备刚被退出，avibe.bot 还在结束它原来的登录。过几分钟再登录一次。',
            'This device was just signed out, and avibe.bot is still ending its earlier sign-in. Sign in again in a few minutes.',
          )} />
        </p>
        <p className="mt-2 text-[14px] leading-relaxed text-muted">
          <Para text={d(
            '若一直不行，先在 avibe.bot 退出登录，再回到这里登录。',
            'If it keeps failing, sign out at avibe.bot first, then come back here and sign in.',
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
  return (
    <SettingsPanel>
      <div className="flex flex-col gap-3 px-5 py-4">
        <div className="flex items-center gap-2">
          <Badge variant="success"><Check className="size-3" />{d('已开启', 'On')}</Badge>
          <span className="text-[14px] font-semibold text-foreground">{d('远程访问已开启，请打开新地址', 'Remote access is on. Open the new address')}</span>
        </div>
        <p className="text-[12px] leading-relaxed text-muted">
          <Para text={d(
            '这个页面是从局域网打开的，绑定之后它不再被认作这台电脑，读不到新的状态。接下来请用新地址打开这个 Avibe：',
            'This page was opened over the local network; once bound, it is no longer treated as this computer and cannot read the new state. From now on, open this Avibe at its new address:',
          )} />
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <a href={`https://${host}`} className="font-mono text-[14px] font-semibold text-cyan-ink hover:underline">{`https://${host}`}</a>
          <Button variant="ghost" size="2xs" aria-label={d('复制地址', 'Copy address')}><Copy /></Button>
        </div>
        <div><Button variant="brand"><ExternalLink />{d('打开新地址', 'Open the new address')}</Button></div>
      </div>
    </SettingsPanel>
  );
}
