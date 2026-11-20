// Proposed 2026-11-20 (frames only, a draft to discuss): Settings · Devices, the one page for
// remote access, the devices that reach this Avibe, what each offers, and recent activity
// (ui/design: settings/devices/*). It takes the place of Settings · Remote access.
// Three layers: how this Avibe is reached (set once), the devices (one row each, a detail with
// its functions and one way out), and recent activity. No pairing key anywhere: remote access
// uses the sign-in this device already has. Draft copy, inline in both languages.
import type { ReactNode } from 'react';
import {
  AlarmClock, Bell, ChevronRight, Copy, ExternalLink, Globe, HeartPulse, Laptop, LogOut, Monitor,
  MonitorCog, RefreshCw, Smartphone,
} from 'lucide-react';

import { accountInitialFor } from '@/components/AccountAvatar';
import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '@/components/settings/SettingsPrimitives';
import { StatusDot } from '@/components/SidebarStatusRow';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { SegmentedRadio } from '@/components/ui/segmented';
import { useDraft } from './draft';

type D = (zh: string, en: string) => string;

export type PathKind = 'tunnel' | 'lan' | 'punched' | 'relay';
export type CapabilityKind = 'notifications' | 'health' | 'schedule' | 'computerUse';
export type DeviceKind = 'host' | 'iphone' | 'desktop' | 'browser';

export type Capability = {
  kind: CapabilityKind;
  on: boolean;
  /** One line under the name: what it does, or its last use. */
  detail: { zh: string; en: string };
  /** The switch cannot be changed from here (Computer Use seen from another device). */
  fixed?: { zh: string; en: string };
};

export type Device = {
  id: string;
  kind: DeviceKind;
  name: string;
  /** What runs there: "Avibe App 3.2.8 · iOS 26.1", "Chrome · macOS". */
  app: string;
  /** The device this page is open on. */
  self?: boolean;
  /** Connected now, and on which path; absent when it is not connected. */
  online?: { path?: PathKind; ms?: number };
  /** Shown when it is not connected. */
  lastActive?: { zh: string; en: string };
  signedIn?: { zh: string; en: string };
  capabilities: Capability[];
  /** In the Avibe App, on the device itself: its own choice of path (the App keeps it). */
  pathChoice?: 'auto' | 'tunnel';
  /** Someone else's device on a shared instance. */
  person?: string;
  recent?: Activity[];
};

export type Activity = { at: { zh: string; en: string }; text: { zh: string; en: string }; device?: string };

export type Reach =
  /** A plain browser on the computer that runs a standalone Runtime: the Runtime knows no account. */
  | { state: 'off'; surface: 'browser'; error?: 'unreachable' }
  /** The Desktop App on this computer: its own account sign-in is the sign-in. */
  | { state: 'off'; surface: 'desktop'; account: string | null; name: string; nameState?: 'free' | 'taken' }
  /** Back from avibe.bot (or after the App's sign-in): turning on. */
  | { state: 'starting'; host: string; account: string }
  | { state: 'on'; host: string; account: string; tunnel: 'good' | 'down'; ms?: number; direct?: boolean }
  /** Bound to the account, switched off: the domain is kept. */
  | { state: 'paused'; host: string; account: string }
  /** This computer is bound to another account than the one that just confirmed. */
  | { state: 'conflict'; host: string; owner: string; account: string };

export type DevicesPageProps = {
  reach: Reach;
  devices: Device[];
  activity?: Activity[];
  /** A member of a shared instance sees only their own devices and no instance controls. */
  viewer?: 'owner' | 'member';
  ownerEmail?: string;
  /** The device whose detail is open. */
  detail?: string;
  /** The detail's way out was pressed and something would be lost: the confirmation. */
  confirm?: 'remove' | 'turn-off' | 'remove-others';
  /** The full activity list in place of the page. */
  view?: 'page' | 'activity';
};

const pathName = (d: D): Record<PathKind, string> => ({
  tunnel: d('Cloudflare 隧道', 'Cloudflare tunnel'),
  lan: d('局域网直连', 'LAN direct'),
  punched: d('打洞直连', 'Hole-punched direct'),
  relay: d('中继', 'Relay'),
});

const capabilityName = (d: D): Record<CapabilityKind, string> => ({
  notifications: d('通知', 'Notifications'),
  health: d('健康', 'Health'),
  schedule: d('闹钟与提醒', 'Alarms and reminders'),
  computerUse: d('电脑操作', 'Computer Use'),
});

const capabilityIcon = { notifications: Bell, health: HeartPulse, schedule: AlarmClock, computerUse: MonitorCog } as const;
const deviceIcon = { host: Laptop, iphone: Smartphone, desktop: Monitor, browser: Globe } as const;

const pick = (d: D, text: { zh: string; en: string }) => d(text.zh, text.en);

/** The account, drawn as the App's own account row draws it: the initial and the email. */
function Account({ email, note }: { email: string; note?: string }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-mint-soft text-[11px] font-semibold text-mint-ink">
        {accountInitialFor(email)}
      </span>
      <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{email}</span>
      {note && <span className="shrink-0 text-[11px] text-muted">{note}</span>}
    </span>
  );
}

function IconTile({ children, tone = 'plain' }: { children: ReactNode; tone?: 'plain' | 'on' }) {
  return (
    <span className={`flex size-9 shrink-0 items-center justify-center rounded-[10px] ${tone === 'on' ? 'bg-mint-soft text-mint-ink' : 'bg-field text-muted'}`}>
      {children}
    </span>
  );
}

const lead = (d: D) => d(
  '在手机和其他电脑上打开这个 Avibe。数据和 Agent 仍然留在这台电脑上。',
  'Open this Avibe from your phone and other computers. Your data and Agents stay on this computer.',
);

/** Layer 1: how this Avibe is reached. Set once, so it is compact once it is on. */
function ReachCard({ reach }: { reach: Reach }) {
  const d = useDraft();
  if (reach.state === 'off' && reach.surface === 'browser') {
    return (
      <SettingsPanel title={d('远程访问', 'Remote access')} description={lead(d)} actions={<Badge variant="secondary">{d('未开启', 'Off')}</Badge>}>
        <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center">
          <Button variant="brand">{d('开启远程访问', 'Turn on remote access')}</Button>
          <p className="text-[12px] leading-relaxed text-muted">
            {d(
              '接下来在 avibe.bot 给这台电脑起个名字、领取域名，然后自动回到这里。',
              'Next, on avibe.bot, you name this computer and get its domain, then come back here by itself.',
            )}
          </p>
        </div>
        {reach.error === 'unreachable' && (
          <p className="border-t border-border px-5 py-3 text-[12px] text-destructive-ink">
            {d('暂时连不上 avibe.bot。检查这台电脑的网络后再试。', 'avibe.bot cannot be reached right now. Check this computer’s network and try again.')}
          </p>
        )}
      </SettingsPanel>
    );
  }
  if (reach.state === 'off') {
    const signedIn = reach.account !== null;
    return (
      <SettingsPanel title={d('远程访问', 'Remote access')} description={lead(d)} actions={<Badge variant="secondary">{d('未开启', 'Off')}</Badge>}>
        {signedIn ? (
          <>
            <div className="grid gap-x-6 gap-y-2 border-b border-border px-5 py-4 md:grid-cols-[72px_minmax(0,1fr)] md:items-center md:gap-y-3">
              <span className="text-[12px] text-muted">{d('账号', 'Account')}</span>
              <Account email={reach.account ?? ''} note={d('这个 App 已登录', 'signed in to this App')} />
              <span className="text-[12px] text-muted max-md:mt-2">{d('名称', 'Name')}</span>
              <div className="flex min-w-0 items-center gap-2">
                <Input className="w-44 text-[13px]" value={reach.name} readOnly aria-label={d('名称', 'Name')} />
                <span className="truncate font-mono text-[12px] text-muted">-app.avibe.bot</span>
              </div>
              <span className="max-md:hidden" />
              {reach.nameState === 'taken' ? (
                <span className="text-[11px] text-destructive-ink md:-mt-1.5">{d('这个名称已被使用，换一个。', 'That name is taken. Choose another.')}</span>
              ) : (
                <span className="text-[11px] text-muted md:-mt-1.5">
                  {d(`域名是 ${reach.name}-app.avibe.bot，领取后不能更改。`, `The domain is ${reach.name}-app.avibe.bot. It cannot be changed later.`)}
                </span>
              )}
            </div>
            <div className="px-5 py-4">
              <Button variant="brand" disabled={reach.nameState === 'taken'}>{d('开启远程访问', 'Turn on remote access')}</Button>
            </div>
          </>
        ) : (
          <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center">
            <Button variant="brand">{d('登录并开启', 'Sign in and turn on')}</Button>
            <p className="text-[12px] leading-relaxed text-muted">
              {d(
                '用这个 App 的 Avibe 账号登录一次，回到这里起个名字就能开启。',
                'Sign in to this App with your Avibe account once, then name this computer here.',
              )}
            </p>
          </div>
        )}
      </SettingsPanel>
    );
  }
  if (reach.state === 'conflict') {
    return (
      <SettingsPanel
        title={d('这台电脑已属于另一个账号', 'This computer belongs to another account')}
        description={d(
          `${reach.host} 登记在 ${reach.owner} 名下，原有的域名和访问设置都没有变。`,
          `${reach.host} is registered to ${reach.owner}. Its domain and access settings are unchanged.`,
        )}
        actions={<Badge variant="warning">{d('未开启', 'Off')}</Badge>}
      >
        <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center">
          <Button variant="secondary">{d(`改用 ${reach.owner} 登录`, `Sign in as ${reach.owner}`)}</Button>
          <p className="text-[12px] leading-relaxed text-muted">
            {d(`刚才确认的是 ${reach.account}。`, `You confirmed as ${reach.account}.`)}
          </p>
        </div>
      </SettingsPanel>
    );
  }
  const on = reach.state === 'on';
  const starting = reach.state === 'starting';
  const down = on && reach.tunnel === 'down';
  const status = starting
    ? d('正在开启…', 'Turning on…')
    : reach.state === 'paused'
      ? d('已关闭，域名为你保留', 'Off. The domain is kept for you')
      : down
        ? d('连不上 Cloudflare，其他设备暂时打不开', 'Cloudflare cannot be reached; other devices cannot open it for now')
        : [
            d('连接良好', 'Connection good') + (reach.state === 'on' && reach.ms ? ` · ${reach.ms} ms` : ''),
            reach.state === 'on' && reach.direct ? d('直连已开启', 'Direct on') : null,
          ].filter(Boolean).join(' · ');
  return (
    <SettingsPanel>
      <div className="flex items-center gap-3.5 px-5 py-4">
        <IconTile tone={on && !down ? 'on' : 'plain'}><Globe size={18} /></IconTile>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1">
            <span className={`truncate font-mono text-[14px] font-semibold ${reach.state === 'paused' ? 'text-muted' : 'text-foreground'}`}>{reach.host}</span>
            {on && (
              <>
                <Button variant="ghost" size="2xs" aria-label={d('复制地址', 'Copy address')}><Copy /></Button>
                <Button variant="ghost" size="2xs" aria-label={d('打开', 'Open')}><ExternalLink /></Button>
              </>
            )}
          </div>
          <div className="mt-0.5 flex min-w-0 flex-col gap-x-1.5 text-[11px] leading-relaxed text-muted md:flex-row md:items-center">
            <span className="truncate">{reach.account}</span>
            <span aria-hidden className="max-md:hidden">·</span>
            <span className={down ? 'text-gold-ink' : undefined}>{status}</span>
          </div>
        </div>
        {down && <Button variant="secondary" size="xs"><RefreshCw />{d('重试', 'Retry')}</Button>}
        <div className="flex shrink-0 items-center gap-2.5">
          <span className="hidden text-[12px] text-muted md:inline">{d('远程访问', 'Remote access')}</span>
          <ToggleSwitch enabled={on || starting} disabled={starting} onClick={() => undefined} label={d('远程访问', 'Remote access')} />
        </div>
      </div>
      {on && (
        <div className="flex items-center justify-between border-t border-border px-5 py-2.5 text-[12px] text-muted">
          <span>{d('连接方式与诊断', 'Connection and diagnostics')}</span>
          <ChevronRight className="size-4" />
        </div>
      )}
    </SettingsPanel>
  );
}

function DeviceRow({ device }: { device: Device }) {
  const d = useDraft();
  const Icon = deviceIcon[device.kind];
  const offered = device.capabilities.filter((each) => each.on).map((each) => capabilityName(d)[each.kind]);
  const second = [device.person, device.app, offered.join(d('、', ', '))].filter(Boolean).join(' · ');
  // Where it is now: the path its connection uses, or when it was last here. The computer the page
  // is open on says nothing: the page being open is the fact.
  const where = device.self && !device.online?.path ? null : device.online ? (
    <>
      <StatusDot tone="running" />
      <span className="text-foreground">{device.online.path ? pathName(d)[device.online.path] : d('在线', 'Online')}</span>
    </>
  ) : device.lastActive ? <span>{pick(d, device.lastActive)}</span> : null;
  return (
    <div className="flex items-center gap-3.5 border-b border-border px-5 py-3 last:border-b-0">
      <IconTile><Icon size={18} /></IconTile>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{device.name}</span>
          {device.self && <Badge variant="info">{d('本设备', 'This device')}</Badge>}
          {device.kind === 'host' && <Badge variant="secondary">{d('运行 Avibe', 'Runs Avibe')}</Badge>}
        </div>
        <div className="mt-0.5 text-[11px] leading-relaxed text-muted md:truncate">{second}</div>
        {where && <div className="mt-1 flex items-center gap-1.5 text-[11px] text-muted md:hidden">{where}</div>}
      </div>
      {where && <div className="hidden shrink-0 items-center gap-1.5 text-[12px] text-muted md:flex">{where}</div>}
      <ChevronRight className="size-4 shrink-0 text-muted" />
    </div>
  );
}

function ActivityRow({ item, wide }: { item: Activity; wide?: boolean }) {
  const d = useDraft();
  return (
    <div className="flex items-baseline gap-3 border-b border-border px-5 py-2.5 last:border-b-0">
      <span className={`shrink-0 text-[11px] tabular-nums text-muted ${wide ? 'w-24' : 'w-20'}`}>{pick(d, item.at)}</span>
      <span className="min-w-0 flex-1 text-[12px] leading-relaxed text-foreground">{pick(d, item.text)}</span>
      {item.device && <span className="hidden shrink-0 text-[11px] text-muted md:inline">{item.device}</span>}
    </div>
  );
}

/** A device's detail: where it is, what it offers (a switch each), what it did lately, one way out. */
function DeviceDetail({ device, ownerEmail }: { device: Device; ownerEmail?: string }) {
  const d = useDraft();
  const Icon = deviceIcon[device.kind];
  const host = device.kind === 'host';
  const facts: Array<[string, string]> = [];
  if (device.online) {
    facts.push([d('连接', 'Connection'), [
      device.online.path ? pathName(d)[device.online.path] : d('在线', 'Online'),
      device.online.ms ? `${device.online.ms} ms` : null,
    ].filter(Boolean).join(' · ')]);
  } else if (device.lastActive) {
    facts.push([d('最近活跃', 'Last active'), pick(d, device.lastActive)]);
  }
  if (device.signedIn) facts.push([d('登录', 'Signed in'), pick(d, device.signedIn)]);
  return (
    <Dialog open onOpenChange={() => undefined}>
      <DialogContent className="max-w-[480px] gap-0 p-0 max-md:p-0" closeLabel={d('关闭', 'Close')}>
        <DialogHeader className="px-6 pb-4 pt-6 max-md:px-5 max-md:pt-3">
          <div className="flex items-center gap-3.5">
            <IconTile><Icon size={18} /></IconTile>
            <div className="min-w-0 text-left">
              <DialogTitle className="truncate text-[15px]">{device.name}</DialogTitle>
              <DialogDescription className="mt-0.5 truncate text-[12px]">{[device.person, device.app].filter(Boolean).join(' · ')}</DialogDescription>
            </div>
          </div>
        </DialogHeader>
        {facts.length > 0 && (
          <dl className="grid grid-cols-[72px_minmax(0,1fr)] gap-x-4 gap-y-1.5 border-t border-border px-6 py-3.5 text-[12px] max-md:px-5">
            {facts.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-muted">{label}</dt>
                <dd className="text-foreground">{value}</dd>
              </div>
            ))}
          </dl>
        )}
        {device.pathChoice && (
          <div className="flex items-center justify-between gap-3 border-t border-border px-6 py-3 max-md:px-5">
            <span className="text-[12px] text-muted">{d('这台设备走', 'This device uses')}</span>
            <SegmentedRadio
              ariaLabel={d('这台设备使用的路径', 'Path for this device')}
              value={device.pathChoice}
              onChange={() => undefined}
              options={[
                { id: 'auto', label: d('自动', 'Automatic') },
                { id: 'tunnel', label: d('仅 Cloudflare', 'Cloudflare only') },
              ]}
            />
          </div>
        )}
        {device.capabilities.length > 0 && (
          <div className="border-t border-border">
            <div className="px-6 pb-1 pt-3.5 text-[11px] font-semibold text-muted max-md:px-5">
              {d('功能', 'Functions')}
            </div>
            {device.capabilities.map((each) => {
              const CapabilityIcon = capabilityIcon[each.kind];
              return (
                <div key={each.kind} className="flex items-center gap-3 px-6 py-2.5 max-md:px-5">
                  <CapabilityIcon className="size-4 shrink-0 text-muted" />
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] font-medium text-foreground">{capabilityName(d)[each.kind]}</div>
                    <div className="text-[11px] leading-relaxed text-muted">{pick(d, each.fixed ?? each.detail)}</div>
                  </div>
                  <ToggleSwitch enabled={each.on} disabled={Boolean(each.fixed)} onClick={() => undefined} label={capabilityName(d)[each.kind]} />
                </div>
              );
            })}
            <div className="h-2" />
          </div>
        )}
        {device.recent && device.recent.length > 0 && (
          <div className="border-t border-border pb-1">
            <div className="px-6 pb-1 pt-3.5 text-[11px] font-semibold text-muted max-md:px-5">{d('最近', 'Recent')}</div>
            {device.recent.map((item) => (
              <div key={item.text.zh} className="flex items-baseline gap-3 px-6 py-1.5 max-md:px-5">
                <span className="w-20 shrink-0 text-[11px] tabular-nums text-muted">{pick(d, item.at)}</span>
                <span className="min-w-0 flex-1 text-[12px] leading-relaxed text-foreground">{pick(d, item.text)}</span>
              </div>
            ))}
          </div>
        )}
        {!host && (
          <div className="flex flex-col gap-2 border-t border-border px-6 py-4 max-md:px-5 md:flex-row md:items-center md:justify-between">
            <p className="text-[11px] leading-relaxed text-muted md:max-w-[250px]">
              {device.person
                ? d(
                    `只结束这台设备的访问。${device.person} 仍在访问名单里，可以重新登录。`,
                    `Ends this device’s access only. ${device.person} is still on the access list and can sign in again.`,
                  )
                : d(
                    `这台设备会退出 ${ownerEmail ?? ''}，不能再打开你的 Avibe；在它上面重新登录即可恢复。`,
                    `This device is signed out of ${ownerEmail ?? ''} and can no longer open your Avibe. Sign in on it again to restore it.`,
                  )}
            </p>
            <Button variant="destructive-soft" size="sm" className="shrink-0"><LogOut />{d('退出这台设备', 'Sign out this device')}</Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Confirm({ title, body, action }: { title: string; body: string; action: string }) {
  const d = useDraft();
  return (
    <Dialog open onOpenChange={() => undefined}>
      <DialogContent className="max-w-md" closeLabel={d('关闭', 'Close')}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost">{d('取消', 'Cancel')}</Button>
          <Button variant="destructive">{action}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DevicesPage({ reach, devices, activity = [], viewer = 'owner', ownerEmail, detail, confirm, view = 'page' }: DevicesPageProps) {
  const d = useDraft();
  const mine = devices.filter((each) => !each.person);
  const others = devices.filter((each) => each.person);
  const opened = devices.find((each) => each.id === detail);
  const on = reach.state === 'on';
  const signedIn = mine.filter((each) => each.kind !== 'host' && !each.self).length;

  if (view === 'activity') {
    return (
      <SettingsPageShell
        title={d('活动', 'Activity')}
        subtitle={d('登录、退出，以及 Avibe 在各台设备上做过的事。保留 90 天。', 'Sign-ins, sign-outs, and what Avibe did on each device. Kept for 90 days.')}
        activeTab="remote-access"
        breadcrumb={<span>{d('设备 / 活动', 'Devices / Activity')}</span>}
      >
        <SettingsPanel>
          {activity.map((item) => <ActivityRow key={item.at.zh + item.text.zh} item={item} wide />)}
        </SettingsPanel>
      </SettingsPageShell>
    );
  }

  return (
    <SettingsPageShell
      title={d('设备', 'Devices')}
      subtitle={viewer === 'member'
        ? d(`你登录了这个 Avibe 的设备。这个 Avibe 由 ${ownerEmail ?? ''} 管理。`, `The devices you are signed in to this Avibe on. ${ownerEmail ?? ''} manages this Avibe.`)
        : d('哪些设备能打开这个 Avibe，它们各自能做什么。', 'Which devices can open this Avibe, and what each can do.')}
      activeTab="remote-access"
    >
      {viewer === 'owner' && <ReachCard reach={reach} />}

      <SettingsPanel title={d('我的设备', 'My devices')}>
        {mine.map((each) => <DeviceRow key={each.id} device={each} />)}
        {viewer === 'owner' && !on && reach.state !== 'starting' && mine.length === 1 && (
          <p className="px-5 py-3 text-[12px] text-muted">
            {d('开启远程访问后，登录的手机和电脑会出现在这里。', 'Once remote access is on, the phones and computers you sign in on appear here.')}
          </p>
        )}
        {viewer === 'owner' && on && mine.length === 1 && reach.state === 'on' && (
          <div className="flex flex-col gap-1 px-5 py-3.5 text-[12px] leading-relaxed text-muted">
            <span>{d(`在 iPhone 上安装 Avibe App，用 ${reach.account} 登录，它就会出现在这里。`, `Install the Avibe App on your iPhone and sign in as ${reach.account}; it appears here.`)}</span>
            <span>{d(`在别的电脑上，用浏览器打开 ${reach.host}。`, `On another computer, open ${reach.host} in a browser.`)}</span>
          </div>
        )}
        {signedIn > 1 && (
          <div className="flex items-center justify-between gap-3 px-5 py-2.5">
            <span className="text-[11px] text-muted">{d('有不认识的设备？', 'A device you do not recognise?')}</span>
            <Button variant="ghost" size="compact" className="text-destructive-ink">{d('退出其他所有设备', 'Sign out all other devices')}</Button>
          </div>
        )}
      </SettingsPanel>

      {viewer === 'owner' && others.length > 0 && (
        <SettingsPanel
          title={d('其他人的设备', 'Other people’s devices')}
          description={d('谁可以访问、能做什么，在「访问」里管理。', 'Who has access, and what they may do, is managed in Access.')}
        >
          {others.map((each) => <DeviceRow key={each.id} device={each} />)}
        </SettingsPanel>
      )}

      {viewer === 'owner' && activity.length > 0 && (
        <SettingsPanel title={d('最近活动', 'Recent activity')} actions={<Button variant="text" size="compact">{d('全部', 'All')}<ChevronRight /></Button>}>
          {activity.map((item) => <ActivityRow key={item.at.zh + item.text.zh} item={item} />)}
        </SettingsPanel>
      )}

      {opened && !confirm && <DeviceDetail device={opened} ownerEmail={ownerEmail} />}
      {opened && confirm === 'remove' && (
        <Confirm
          title={d(`退出 ${opened.name}？`, `Sign out ${opened.name}?`)}
          body={d(
            '这台 iPhone 同步到这个 Avibe 的健康数据会被删除。重新登录后要在 iPhone 上再授权一次。',
            'The health data this iPhone synced to this Avibe will be deleted. After signing in again, authorize it on the iPhone once more.',
          )}
          action={d('退出并删除健康数据', 'Sign out and delete health data')}
        />
      )}
      {confirm === 'turn-off' && (
        <Confirm
          title={d('关闭远程访问？', 'Turn off remote access?')}
          body={d(
            `你正在用 ${devices.find((each) => each.self)?.name ?? ''} 远程打开它。关闭后这台设备和其他 ${Math.max(0, signedIn)} 台都打不开，只能回到 ${mine[0]?.name ?? ''} 上重新开启。域名会为你保留。`,
            `You are using it remotely from ${devices.find((each) => each.self)?.name ?? ''}. Once off, this device and ${Math.max(0, signedIn)} others cannot open it; turn it back on at ${mine[0]?.name ?? ''}. The domain is kept for you.`,
          )}
          action={d('关闭远程访问', 'Turn off remote access')}
        />
      )}
      {confirm === 'remove-others' && (
        <Confirm
          title={d('退出其他所有设备？', 'Sign out all other devices?')}
          body={d(
            `除了这台设备，其他 ${signedIn} 台都会退出 ${ownerEmail ?? ''}。iPhone 同步的健康数据会被删除。`,
            `Every device but this one, ${signedIn} in all, is signed out of ${ownerEmail ?? ''}. Health data synced from the iPhone is deleted.`,
          )}
          action={d('全部退出', 'Sign out all')}
        />
      )}
    </SettingsPageShell>
  );
}
