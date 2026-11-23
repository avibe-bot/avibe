import type { ReactNode } from 'react';
import { ArrowLeft, ArrowRight, Check, ChevronDown, ChevronRight, CircleHelp, Download, Folder, Globe, Home, Inbox, Laptop, LogOut, RefreshCw, Settings, ShieldCheck, TriangleAlert, UserRound, WifiOff } from 'lucide-react';
import { StatusDot, StatusGlyph } from '@/components/SidebarStatusRow';
import { VersionChip } from '@/components/VersionChip';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { Workbench } from '@/components/Workbench';
import { PetAvatar } from '@/pet/PetAvatar';
import { cn } from '@/lib/utils';
import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';
import { AppsLauncher } from '@/components/AppsLauncher';

export type DesktopInstanceState = 'signed-out' | 'enroll' | 'updates' | 'setup-entry' | 'account' | 'instances' | 'empty' | 'loading' | 'list-error' | 'email' | 'code' | 'code-error' | 'registering' | 'registered' | 'registration-error' | 'conflict' | 'add' | 'advanced' | 'offline' | 'switching' | 'mini' | 'account-remote-disabled' | 'account-long' | 'password' | 'password-help' | 'two-factor' | 'recovery' | 'consent' | 'switch-error' | 'upgrade-error' | 'offline-help' | 'long-content' | 'current-shared' | 'offline-shared' | 'profile' | 'profile-disabled' | 'profile-error' | 'sign-out-error';
export type ConnectionStatus = 'signed-in' | 'signed-out' | 'switching' | 'offline';
export type DesktopInstanceFixture = { state: DesktopInstanceState; lang: 'zh' | 'en'; connection: ConnectionStatus; updateAvailable: boolean };
const copy = (lang: 'zh' | 'en') => (zh: string, en: string) => lang === 'zh' ? zh : en;
type Copy = ReturnType<typeof copy>;
function Heading({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="mb-7"><h1 className="text-2xl font-semibold tracking-tight">{title}</h1>{children && <p className="mt-3 max-w-xl text-sm leading-6 text-muted">{children}</p>}</div>;
}
function Notice({ children, warning = false }: { children: ReactNode; warning?: boolean }) {
  return <div role="status" className={cn('flex gap-3 rounded-xl border p-4 text-sm leading-6', warning ? 'border-gold/40 bg-gold/10 text-foreground' : 'border-edge bg-field text-muted')}><span className="mt-0.5 shrink-0">{warning ? <TriangleAlert className="size-4 text-gold-ink" /> : <CircleHelp className="size-4" />}</span><div>{children}</div></div>;
}
function Account({ t, upward = false }: { t: Copy; upward?: boolean }) {
  return <div data-instance-menu className={cn("absolute z-60 w-80 rounded-xl border border-edge bg-popover p-2 text-popover-foreground shadow-elevation-2", upward ? "bottom-full left-0 mb-2" : "top-full left-0 mt-2")}><div className="flex items-center gap-3 px-3 py-3"><UserRound className="size-5 text-muted" /><div><p className="text-sm font-semibold">max@example.com</p><p className="mt-1 text-xs text-muted">avibe.bot</p></div></div><div className="border-y border-edge py-1"><Button variant="ghost" className="h-auto w-full justify-start gap-3 px-3 py-3"><Globe className="size-4" /><span className="flex-1 text-left">{t('切换实例', 'Switch instance')}</span><span className="text-xs text-muted">Studio</span><ChevronRight className="size-4" /></Button></div><div className="flex items-center gap-3 px-3 py-3"><Laptop className="size-4 text-muted" /><div><p className="text-sm">{t('本机远程访问', 'Remote access to this Mac')}</p><p className="mt-1 text-xs text-muted">{t('MacBook Pro · 已开启', 'MacBook Pro · On')}</p></div></div><Button variant="ghost" className="w-full justify-start gap-3 text-muted"><LogOut className="size-4" />{t('退出账号', 'Sign out')}</Button></div>;
}
function Registration({ fixture }: { fixture: DesktopInstanceFixture }) {
  const t = copy(fixture.lang), conflict = fixture.state === 'conflict', busy = fixture.state === 'registering', error = fixture.state === 'registration-error';
  return <div className="mx-auto max-w-xl pt-12"><div className="mb-6 grid size-14 place-items-center rounded-2xl border border-edge bg-field">{conflict || error ? <TriangleAlert className="size-6 text-gold-ink" /> : busy ? <Laptop className="size-6 text-muted" /> : <ShieldCheck className="size-6 text-mint-ink" />}</div><Heading title={conflict ? t('本机已属于另一个账号', 'This Mac is linked to another account') : busy ? t('正在把本机加入账号', 'Adding this Mac to your account') : error ? t('本机已加入，远程访问尚未就绪', 'Added to your account. Remote access is not ready.') : t('本机已加入你的账号', 'This Mac is now in your account')}>{conflict ? t('当前登录 max@example.com。本机原有绑定会保留，直到你明确选择改绑。', 'You signed in as max@example.com. The existing link stays until you choose to change it.') : 'MacBook Pro · max@example.com'}</Heading>
    {conflict ? <><Notice warning>{t('改绑会改变这台电脑的远程访问归属，原账号与共享成员可能失去访问。需验证原有归属或通过恢复流程。', 'Changing the linked account can remove access for the previous owner and shared members. Ownership verification or recovery is required.')}</Notice><div className="mt-7 flex flex-wrap gap-3"><Button variant="brand">{t('保持绑定，仅使用本机', 'Keep link · use locally')}</Button><Button variant="outline">{t('查看改绑步骤', 'Review account change')}</Button></div></> : busy ? <><div className="space-y-4"><p className="flex items-center gap-2 text-sm"><Check className="size-4 text-mint-ink" />{t('已登录', 'Signed in')}</p><Skeleton className="h-5 w-3/4" /><p className="text-sm text-muted">{t('登记本机，然后开启远程访问…', 'Registering this Mac, then enabling remote access…')}</p></div><Button variant="ghost" className="mt-7">{t('先使用本机', 'Use this Mac for now')}</Button></> : <><div className="rounded-xl border border-edge bg-field p-5"><div className="flex items-center justify-between gap-3"><p className="text-sm font-medium">{t('远程访问', 'Remote access')}</p><Badge variant={error ? 'warning' : 'success'}>{error ? t('未连接', 'Not connected') : t('已开启', 'On')}</Badge></div><p className="mt-3 text-sm leading-6 text-muted">{error ? t('检查网络后重试。本机可以照常使用，不会重复登记。', 'Check your connection and retry. This Mac works locally and will not be registered twice.') : t('现在可在手机或其他电脑上访问本机；开关留在设置里的远程访问页面。', 'You can now reach this Mac from your phone or another computer; the switch stays in Settings → Remote access.')}</p></div><div className="mt-7 flex gap-3"><Button variant="brand">{error ? t('重试远程访问', 'Retry remote access') : t('查看实例', 'View instances')}</Button><Button variant="ghost">{t('继续使用本机', 'Continue on this Mac')}</Button></div></>}
  </div>;
}
function Advanced({ fixture }: { fixture: DesktopInstanceFixture }) {
  const t = copy(fixture.lang);
  return <div className="mx-auto max-w-xl pt-6"><Button variant="ghost" size="sm" className="mb-5 -ml-3"><ArrowLeft className="size-4" />{t('实例', 'Instances')}</Button><Heading title={t('通过 URL 连接', 'Connect by URL')}>{t('高级 / 开发入口。仍需使用当前账号验证访问权限。', 'For advanced use and development. Access is still verified with your account.')}</Heading><Label htmlFor="instance-url">{t('实例 URL', 'Instance URL')}</Label><Input id="instance-url" className="mt-2 h-12" defaultValue="https://studio-app.avibe.bot" /><p className="mt-3 text-xs leading-6 text-muted">{t('仅接受 avibe.bot 子域的 HTTPS 地址。不会绕过账号授权或开放本机权限。', 'Only HTTPS avibe.bot subdomains. Account authorization and local permissions still apply.')}</p><Button variant="brand" className="mt-6">{t('验证并连接', 'Verify and connect')}</Button></div>;
}
function Mini({ t }: { t: Copy }) {
  return <div className="absolute right-10 bottom-8 z-10 flex items-end gap-3"><div className="w-80 rounded-2xl border border-edge bg-popover p-5 shadow-elevation-2"><div className="mb-3 flex items-center justify-between gap-2"><span className="text-sm font-semibold">Vibey</span><span className="flex items-center gap-1.5 text-xs text-muted"><Globe className="size-3" />Studio</span></div><p className="text-sm leading-6">{t('方案已整理好，等你看看。', 'The proposal is ready for you to review.')}</p><Button variant="text" size="sm" className="mt-3 px-0">{t('打开对话', 'Open conversation')}<ArrowRight className="size-4" /></Button></div><PetAvatar pose="ready" badge={1} /></div>;
}
function SetupEntry({ t }: { t: Copy }) {
  return <div className="mx-auto max-w-xl pt-12"><ShieldCheck className="mb-6 size-10 text-mint-ink" /><Heading title={t('本机已准备好', 'This Mac is ready')}>{t('现在就可以和伙伴开始工作。想从手机或其他电脑访问？登录后即可把本机加入账号。', 'You can start working with your Bot now. Sign in to add this Mac to your account and reach it from your phone or another computer.')}</Heading><Notice>{t('登录将登记本机并开启远程访问；开关留在设置里的远程访问页面。', 'Signing in registers this Mac and enables remote access; the switch stays in Settings → Remote access.')}</Notice><div className="mt-7 flex flex-wrap gap-3"><Button variant="brand">{t('开始使用本机', 'Start using this Mac')}</Button><Button variant="outline">{t('登录并登记本机', 'Sign in and add this Mac')}<ArrowRight className="size-4" /></Button></div><p className="mt-5 text-sm text-muted">{t('也可以稍后从侧栏最底部登录。', 'You can also sign in later from the bottom of the sidebar.')}</p></div>;
}
function DesktopUpdatePopover({ t }: { t: Copy }) {
  return <div data-update-menu className="absolute bottom-full left-0 z-60 mb-2 w-80 rounded-xl border border-edge bg-popover p-4 text-popover-foreground shadow-elevation-2"><div className="flex items-center gap-2"><Download className="size-4 text-gold-ink" /><p className="text-sm font-semibold">{t('Desktop 有更新', 'Desktop update available')}</p></div><p className="mt-2 text-xs leading-5 text-muted">{t('这台 Mac 的 Avibe Desktop。当前连接的 Studio 不受影响。', 'Avibe Desktop on this Mac. The connected Studio is unchanged.')}</p><div className="mt-4 flex flex-wrap gap-2"><Button variant="brand" size="sm">{t('安装更新', 'Install update')}</Button><Button variant="outline" size="sm">{t('检查更新', 'Check for updates')}</Button><Button variant="link" size="sm" className="px-0">{t('查看内容', 'View notes')}</Button></div></div>;
}
export type InstancePlacement = 'top' | 'bottom';

function AccountEntry({ fixture, placement, remote, compact = false, menu = false, updatePanel = false }: {
  fixture: DesktopInstanceFixture; placement: InstancePlacement; remote: boolean; compact?: boolean; menu?: boolean; updatePanel?: boolean;
}) {
  const t = copy(fixture.lang), { connection, updateAvailable } = fixture;
  const signedOut = connection === 'signed-out', busy = connection === 'switching';
  const instance = remote ? 'Studio' : t('本机 · MacBook Pro', 'This Mac · MacBook Pro');
  const indicator = connection === 'offline' ? <WifiOff data-status-indicator className="size-3 shrink-0 text-gold-ink" aria-label={t('当前实例已断开', 'Current instance disconnected')} /> : busy ? <RefreshCw data-status-indicator className="size-3 shrink-0 text-cyan-ink" aria-label={t('正在连接 Studio', 'Connecting to Studio')} /> : placement === 'bottom' ? <StatusDot tone="running" label={t('当前实例已连接', 'Current instance connected')} /> : null;
  // The bottom row follows the shipped shared row (ui/design: chrome/bottom-row): the status leads.
  const bottomRow = placement === 'bottom' && !compact;
  return <div data-placement-entry={placement} data-connection-status={connection} data-update-available={updateAvailable} data-compact={compact} className="relative min-w-0">
    <div className={cn('flex items-center gap-1', compact && 'gap-0.5')}>
      <Button data-account-trigger variant="ghost" className={cn('min-w-0 flex-1 justify-start gap-1.5 rounded-md px-0 text-left text-xs', bottomRow && 'gap-1.5', placement === 'top' && 'h-10 px-0 text-xs', placement === 'bottom' ? 'h-8' : 'h-10', compact && 'flex-none justify-center')} title={signedOut ? t('登录 · 本机', 'Sign in · This Mac') : busy ? t('当前本机，正在连接 Studio', 'Current: This Mac, connecting to Studio') : `${instance}${connection === 'offline' ? t(' · 已断开', ' · Disconnected') : ''}`} aria-expanded={menu}>
        {bottomRow ? <StatusGlyph label={busy ? t('本机 → Studio', 'This Mac → Studio') : signedOut ? t('登录或注册', 'Sign in / sign up') : instance}>{indicator}</StatusGlyph> : <UserRound data-account-avatar className="size-3.5 shrink-0 text-muted" />}
        {!compact && placement === 'top' && <span className="min-w-0 flex-1"><span className="block truncate text-xs">{signedOut ? t('登录或注册', 'Sign in / sign up') : instance}</span><span className="block truncate text-xs font-normal text-muted">{busy ? t('本机 → Studio · 连接中', 'This Mac → Studio · Connecting') : instance}</span></span>}
        {bottomRow && <span data-account-group className="flex min-w-0 flex-1 items-center gap-2"><span data-account-label className="min-w-0 truncate">{busy ? t('本机 → Studio', 'This Mac → Studio') : signedOut ? t('登录或注册', 'Sign in / sign up') : instance}</span><ChevronDown data-account-chevron className="size-3 shrink-0 rotate-180 text-muted" /></span>}
        {compact && indicator}
        {!compact && placement === 'top' && <>{indicator}<ChevronDown className="size-3 shrink-0 text-muted" /></>}
      </Button>
      {bottomRow && <div className="relative shrink-0"><VersionChip data-version-trigger data-update-trigger={updateAvailable || undefined} aria-label={updateAvailable ? t('Desktop 有更新', 'Desktop update available') : t('本机 Desktop 版本 v3.2.6rc9', 'Local Desktop version v3.2.6rc9')} attention={updateAvailable} updateDot={updateAvailable} aria-expanded={updatePanel}>{updateAvailable && <Download size={12} aria-hidden="true" />}v3.2.6rc9</VersionChip></div>}
      {compact && updateAvailable && placement === 'bottom' && <div className="relative shrink-0"><Button data-version-trigger data-update-trigger={updateAvailable || undefined} aria-label={updateAvailable ? t('Desktop 有更新', 'Desktop update available') : t('本机 Desktop 版本 3.2.6rc9', 'Local Desktop version 3.2.6rc9')} variant="ghost" size="sm" className={cn("h-8 gap-1 px-1.5 text-2xs text-muted", compact && "px-0")} aria-expanded={updatePanel}>{updateAvailable ? <><Download className="size-3 text-gold-ink" />{!compact && <span className="size-1.5 rounded-full bg-gold" aria-hidden="true" />}</> : <span>3.2.6rc9</span>}</Button></div>}
    </div>
    {updateAvailable && updatePanel && placement === 'bottom' && <DesktopUpdatePopover t={t} />}
    {menu && <Account t={t} upward={placement === 'bottom'} />}
  </div>;
}

/** Proposed placement only. Shipped tree, Inbox and Apps launcher remain real. C replaces the former version row. */
export function PlacementShell({ fixture, placement = 'bottom', remote = true, compact = false, menu = false, updatePanel = false, page, flush = false }: {
  fixture: DesktopInstanceFixture; placement?: InstancePlacement; remote?: boolean; compact?: boolean; menu?: boolean; updatePanel?: boolean; page: ReactNode; flush?: boolean;
}) {
 const t = copy(fixture.lang);
 const entry = <AccountEntry fixture={fixture} placement={placement} remote={remote} compact={compact} menu={menu} updatePanel={false} />;
 return <div className="relative flex h-full flex-col bg-background text-foreground" lang={fixture.lang}>
   <div data-native-safe-strip className="pointer-events-none relative z-60 h-7 shrink-0"><div className={cn('absolute inset-y-0 left-0 bg-surface', compact ? 'w-16' : 'w-(--app-sidebar-w)')} /></div>
   <div className="flex min-h-0 flex-1">
     <aside data-placement-sidebar className={cn('relative flex shrink-0 flex-col justify-between gap-6 border-r border-border bg-(--sidebar-background) pb-4 pt-2.5', compact ? 'w-16 pl-4 pr-2' : 'w-(--app-sidebar-w) px-4')}>
       {compact ? <div className="flex flex-1 flex-col gap-4">{placement === 'top' && entry}<Button variant="ghost" size="icon" aria-label={t('首页', 'Home')}><Home className="size-4" /></Button><Button variant="ghost" size="icon" aria-label={t('收件箱', 'Inbox')}><Inbox className="size-4" /></Button><Button variant="ghost" size="icon" aria-label={t('项目', 'Projects')}><Folder className="size-4" /></Button></div> : <div className={cn('relative flex min-h-0 flex-1 flex-col', placement === 'top' && '[&>div>div:first-child>a]:invisible')}>
         <WorkbenchSidebar />
         {placement === 'top' && <div className="absolute top-0 left-0 right-10 z-20">{entry}</div>}
       </div>}
       <div data-sidebar-footer className="relative flex shrink-0 flex-col gap-2">
         {compact ? <Button variant="outline" size="icon" aria-label={t('设置', 'Settings')}><Settings className="size-4" /></Button> : /* Mirrors AppShell.tsx:505 at 366b89ce13; U1 owns the real footer slot and metric. */ <div className="flex h-[39px] items-stretch gap-2"><AppsLauncher /><Button variant="outline" size="icon" className="h-auto w-11" aria-label={t('设置', 'Settings')}><Settings className="size-4.5 text-muted" /></Button></div>}
         {placement === 'bottom' && <AccountEntry fixture={fixture} placement="bottom" remote={remote} compact={compact} menu={menu} updatePanel={updatePanel && !compact} />}
       </div>
     </aside>
     <main data-placement-content className={cn('relative min-w-0 flex-1 overflow-auto', !flush && 'px-10 py-8', flush && '-mt-7')}>{page}</main>
   </div>
 </div>;
}

/** Recommended C: no account/instance controls share a page's top-right action region. */
export function DesktopInstances({ fixture }: { fixture: DesktopInstanceFixture }) {
  const t = copy(fixture.lang), { state } = fixture;
  const remote = ['instances', 'offline', 'mini', 'updates'].includes(state);
  const workbench = ['mini', 'updates'].includes(state);
  const page = ['registering', 'registered', 'registration-error', 'conflict'].includes(state) ? <Registration fixture={fixture} /> : state === 'advanced' ? <Advanced fixture={fixture} /> : state === 'setup-entry' ? <SetupEntry t={t} /> : null;
  if (workbench) return <div className="relative h-full"><PlacementShell fixture={fixture} remote={remote} updatePanel={state === 'updates'} page={<Workbench />} />{state === 'mini' && <Mini t={t} />}</div>;
  return <div lang={fixture.lang} className="relative flex h-full flex-col bg-background text-foreground"><div aria-hidden="true" className="h-7 shrink-0 bg-surface" /><div className="flex shrink-0 items-center px-8 py-3"><Button variant="ghost" size="sm"><ArrowLeft className="size-4" />{state === 'setup-entry' ? t('返回设置', 'Back to setup') : t('返回工作台', 'Back to Workbench')}</Button></div><main className="min-h-0 flex-1 overflow-y-auto px-10 pb-5">{page}</main><footer className="shrink-0 border-t border-edge bg-surface px-4 py-2"><div className="w-56"><AccountEntry fixture={fixture} placement="bottom" remote={remote} /></div></footer></div>;
}
