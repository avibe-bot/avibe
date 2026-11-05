import { createContext, useContext, type ReactNode } from 'react';
import {
  ArrowDown, ArrowLeft, ArrowRight, Check, ChevronRight,
  Cloud, ExternalLink, Laptop, LoaderCircle, LogOut, MonitorDown,
  RefreshCw, ShieldCheck, UserRound, WifiOff,
} from 'lucide-react';
import clsx from 'clsx';

import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useTheme } from '@/context/ThemeContext';
import logo from '@/assets/logo.png';
import mascot from '@/assets/cloud-tuanzi-logo-mono.png';
import enBlack from '../assets/apple-sign-in/en-black.png';
import enWhite from '../assets/apple-sign-in/en-white.png';
import zhBlack from '../assets/apple-sign-in/zh-black.png';
import zhWhite from '../assets/apple-sign-in/zh-white.png';
import { PhoneScreen } from '../shell/PhoneScreen';

export type MobileLocale = 'zh' | 'en';
export type WelcomeState = 'default' | 'signing-in' | 'cancelled' | 'network-error' | 'denied';
export type PickerState = 'own' | 'mixed' | 'current' | 'offline' | 'offline-tapped' | 'loading' | 'error' | 'pulling' | 'refreshing';
export interface MobileInstance { id: string; name: string; host: string; owner?: string; online: boolean }
export interface MobileAccount { name: string; email: string }
const Locale = createContext<MobileLocale>('zh');
export function MobileLanguage({ lang, children }: { lang: MobileLocale; children: ReactNode }) {
  return <Locale.Provider value={lang}><div lang={lang} className="h-full">{children}</div></Locale.Provider>;
}
function useCopy() {
  const lang = useContext(Locale);
  return (zh: string, en: string) => lang === 'en' ? en : zh;
}

/** The existing phone canvas, with onboarding chrome replacing Workbench tabs.
 * Native safe areas live outside this content viewport, as with the existing phone frames. */
function Screen({ children, header, overlay }: { children: ReactNode; header?: ReactNode; overlay?: ReactNode }) {
  return <div className="relative h-full [&>div>main]:pb-0">
    <PhoneScreen flat bare header={header ?? <></>} tabBar={<></>} overlay={overlay}>
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-5 py-6">{children}</div>
    </PhoneScreen>
  </div>;
}
function Header({ title, back = false, account = false }: { title?: string; back?: boolean; account?: boolean }) {
  const t = useCopy();
  return <header className="flex min-h-16 shrink-0 items-center gap-2 border-b border-border bg-background px-4">
    {back && <Button size="icon" variant="ghost" className="size-11" aria-label={t('返回', 'Back')}><ArrowLeft className="size-5" /></Button>}
    <div className="flex min-w-0 flex-1 items-center gap-2">
      {!title && <img src={logo} alt="" className="size-8 rounded-lg" />}
      <span className="truncate text-base font-semibold">{title ?? 'Avibe'}</span>
    </div>
    {account && <Button size="icon" variant="ghost" className="size-11" aria-label={t('账户', 'Account')}><UserRound className="size-5" /></Button>}
  </header>;
}
function Primary({ children, disabled = false }: { children: ReactNode; disabled?: boolean }) {
  return <Button size="hero" className="w-full text-base" disabled={disabled}>{children}</Button>;
}
function Secondary({ children }: { children: ReactNode }) {
  return <Button variant="outline" size="lg" className="w-full text-sm">{children}</Button>;
}
function Heading({ title, body }: { title: string; body?: string }) {
  return <div><h1 className="text-2xl font-semibold tracking-tight">{title}</h1>{body && <p className="mt-3 text-sm leading-6 text-muted">{body}</p>}</div>;
}
function AppleButton({ disabled }: { disabled: boolean }) {
  const t = useCopy();
  const lang = useContext(Locale);
  const { resolvedTheme } = useTheme();
  const dark = resolvedTheme === 'dark';
  const src = lang === 'en' ? (dark ? enWhite : enBlack) : (dark ? zhWhite : zhBlack);
  return <Button variant="ghost" className="h-auto w-full overflow-hidden rounded-xl p-0" disabled={disabled} aria-label={t('通过 Apple 登录', 'Sign in with Apple')}>
    <img src={src} alt="" className="block h-auto w-full" />
  </Button>;
}
export function WelcomeIntro({ state = 'default' }: { state?: WelcomeState }) {
  const t = useCopy();
  const pending = state === 'signing-in';
  const notice = state === 'cancelled' ? [t('已取消登录', 'Sign-in cancelled'), t('准备好后，可以重新登录。', 'Sign in again when you are ready.')]
    : state === 'network-error' ? [t('暂时连不上 avibe.bot', 'Could not reach avibe.bot'), t('检查网络连接，然后重试。', 'Check your connection, then try again.')]
    : state === 'denied' ? [t('登录未获授权', 'Sign-in was denied'), t('请重新登录，并允许 Avibe 访问你的账号。', 'Sign in again and allow Avibe to access your account.')]
    : null;
  return <Screen header={<Header />}>
    <div className="flex min-h-full flex-col">
      <div className="flex flex-1 flex-col items-center justify-center py-8 text-center">
        <img src={mascot} alt="" className="size-36 object-contain" />
        <h1 className="mt-6 text-3xl font-semibold tracking-tight">{t('你的伙伴，随时在身边', 'Your Agents, wherever you are')}</h1>
        <p className="mt-4 max-w-xs text-base leading-6 text-muted">{t('登录 avibe.bot，连接你的 Avibe 实例。', 'Sign in to avibe.bot to connect to your Avibe instances.')}</p>
      </div>
      <div className="space-y-4 pb-5">
        {notice && <div role={state === 'cancelled' ? 'status' : 'alert'} className={clsx('rounded-xl border p-4 text-sm', state === 'cancelled' ? 'border-border bg-surface' : 'border-destructive/30 bg-destructive/10')}>
          <p className="font-semibold">{notice[0]}</p><p className="mt-1 leading-5 text-muted">{notice[1]}</p>
        </div>}
        {pending && <div role="status" className="flex gap-3 rounded-xl border border-border bg-surface p-4 text-sm">
          <LoaderCircle className="mt-0.5 size-5 shrink-0 animate-spin" />
          <div><p className="font-semibold">{t('正在等待登录完成', 'Waiting for sign-in')}</p><p className="mt-1 leading-5 text-muted">{t('请在已打开的系统登录面板中继续。完成后会自动返回。', 'Continue in the system sign-in panel. You will return here automatically.')}</p></div>
        </div>}
        <Primary disabled={pending}>{notice ? t('重新登录', 'Try sign-in again') : t('登录 / 注册', 'Sign in / Create account')}<ArrowRight /></Primary>
        <AppleButton disabled={pending} />
        <p className="text-center text-xs leading-5 text-muted">{t('登录 / 注册将在系统面板中打开 avibe.bot。', 'Sign in / Create account opens avibe.bot in a system panel.')}</p>
      </div>
    </div>
  </Screen>;
}
function InstanceRow({ instance, current = false }: { instance: MobileInstance; current?: boolean }) {
  const t = useCopy();
  return <Button variant="ghost" className={clsx('h-auto w-full justify-start gap-3 whitespace-normal rounded-xl border p-4 text-left', current ? 'border-mint/50 bg-mint-soft' : 'border-border bg-surface')}>
    <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-muted-soft text-muted">{instance.online ? <Cloud className="size-5" /> : <WifiOff className="size-5" />}</span>
    <span className="min-w-0 flex-1">
      <span className="block break-words text-base font-semibold">{instance.name}</span>
      <span className="mt-1 block break-all text-xs text-muted">{instance.host}</span>
      <span className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
        <span className="inline-flex items-center gap-1.5"><span className={clsx('size-1.5 rounded-full', instance.online ? 'bg-mint' : 'bg-muted')} />{instance.online ? t('在线', 'Online') : t('离线', 'Offline')}</span>
        <span>{instance.owner ? t(`${instance.owner} 共享`, `Shared by ${instance.owner}`) : t('我的实例', 'Yours')}</span>
      </span>
      {current && <span className="mt-2 inline-flex items-center gap-1 text-xs font-semibold"><Check className="size-3" />{t('当前实例', 'Current instance')}</span>}
    </span>
    <ChevronRight className="size-4 shrink-0 text-muted" />
  </Button>;
}
export function InstancePicker({ state, instances, currentId }: { state: PickerState; instances: MobileInstance[]; currentId?: string }) {
  const t = useCopy();
  const loading = state === 'loading';
  return <Screen header={<Header account />}>
    <Heading title={currentId ? t('切换实例', 'Switch instance') : t('选择实例', 'Choose an instance')} body={t('选择你自己的实例，或别人共享给你的实例。', 'Open one of your instances, or one shared with you.')} />
    <div className="mt-4 flex items-center justify-between">
      <span className="flex items-center gap-1 text-xs text-muted"><ArrowDown className="size-3" />{t('下拉刷新', 'Pull to refresh')}</span>
      <Button variant="ghost" className="min-h-11 text-sm" disabled={loading || state === 'refreshing'}><RefreshCw className="size-4" />{t('刷新', 'Refresh')}</Button>
    </div>
    {(state === 'pulling' || state === 'refreshing') && <div role="status" className="flex h-14 items-center justify-center gap-2 text-sm text-muted">
      {state === 'pulling' ? <ArrowDown className="size-5" /> : <LoaderCircle className="size-5 animate-spin" />}
      {state === 'pulling' ? t('松开即可刷新', 'Release to refresh') : t('正在刷新…', 'Refreshing…')}
    </div>}
    {loading ? <div aria-label={t('正在加载实例', 'Loading instances')} role="status" className="space-y-3">
      {[0,1,2].map(i => <Card key={i} className="flex gap-3 p-4"><div className="size-10 rounded-lg bg-foreground/10 animate-pulse" /><div className="flex-1 space-y-3 py-1"><div className="h-4 w-3/4 rounded bg-foreground/10 animate-pulse" /><div className="h-3 w-1/2 rounded bg-foreground/10 animate-pulse" /><div className="h-3 w-1/3 rounded bg-foreground/10 animate-pulse" /></div></Card>)}
    </div> : state === 'error' ? <div className="flex flex-col items-center py-12 text-center">
      <WifiOff className="size-10 text-muted" /><h2 className="mt-5 text-lg font-semibold">{t('无法加载实例', 'Could not load instances')}</h2>
      <p className="mt-2 text-sm leading-6 text-muted">{t('检查网络连接后重试。', 'Check your connection and try again.')}</p>
      <Button className="mt-6 min-h-12 px-8 text-sm"><RefreshCw />{t('重试', 'Retry')}</Button>
    </div> : <div className="space-y-3">{instances.map(instance => <InstanceRow key={instance.id} instance={instance} current={instance.id === currentId} />)}</div>}
    {state === 'offline-tapped' && <div role="status" className="mt-4 rounded-xl border border-border bg-surface p-4">
      <h2 className="text-base font-semibold">{t('Weekend lab 处于离线状态', 'Weekend lab is offline')}</h2>
      <p className="mt-2 text-sm leading-6 text-muted">{t('在这台电脑上启动 Avibe 并连接网络，再刷新状态。也可以选择其他在线实例。', 'Start Avibe on that computer and connect it to the internet, then refresh. You can also choose another online instance.')}</p>
      <Button variant="outline" className="mt-4 min-h-12 w-full"><RefreshCw />{t('刷新状态', 'Refresh status')}</Button>
    </div>}
    {!loading && state !== 'error' && <Button variant="ghost" className="mt-auto min-h-12 pt-6 text-sm">{t('添加自己的实例', 'Add your own instance')}<ChevronRight className="size-4" /></Button>}
  </Screen>;
}
export function EmptyInstancesPage({ cli = false }: { cli?: boolean }) {
  const t = useCopy();
  return <Screen header={<Header title={cli ? t('安装与配对', 'Install and pair') : undefined} back={cli} account={!cli} />}>
    {cli ? <>
      <Heading title={t('在电脑上安装 Avibe', 'Install Avibe on your computer')} body={t('在 macOS 或 Linux 终端运行。Windows 用户请按指南使用 WSL。', 'Run in Terminal on macOS or Linux. On Windows, follow the guide for WSL.')} />
      <h2 className="mt-7 text-base font-semibold">{t('1. 安装并完成设置', '1. Install and finish setup')}</h2>
      <code className="mt-3 block break-all rounded-xl border border-border bg-surface p-4 text-xs leading-6">bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --launch'</code>
      <h2 className="mt-7 text-base font-semibold">{t('2. 配对你的账号', '2. Pair your account')}</h2>
      <code className="mt-3 block rounded-xl border border-border bg-surface p-4 text-sm">vibe remote</code>
      <p className="mt-3 text-sm leading-6 text-muted">{t('按终端提示登录相同的 avibe.bot 账号，完成配对，再回到手机刷新。', 'Follow the terminal prompts, sign in with the same avibe.bot account, then return here and refresh.')}</p>
      <Button variant="ghost" className="mt-4 min-h-12 text-sm">{t('打开安装指南', 'Open installation guide')}<ExternalLink /></Button>
    </> : <>
      <div className="py-4"><img src={mascot} alt="" className="mb-5 size-20" /><Heading title={t('还没有可用实例', 'No instances yet')} body={t('实例是运行 Avibe 的电脑。先在电脑上安装并配对，就能从手机连接。', 'An instance is a computer running Avibe. Install and pair it, then connect from your phone.')} /></div>
      <Card className="mt-4 p-4"><MonitorDown className="size-6 text-muted" /><h2 className="mt-3 text-lg font-semibold">{t('下载桌面版', 'Download the desktop app')}</h2>
        <p className="mt-2 text-sm leading-6 text-muted">{t('在电脑上安装桌面版，完成设置并在「远程访问」中配对。', 'Install on your computer, finish setup, then pair in Remote Access.')}</p>
        <Button variant="outline" className="mt-4 min-h-12 w-full text-sm">{t('查看桌面版下载', 'View desktop downloads')}<ExternalLink /></Button>
      </Card>
      <div className="mt-5 border-y border-border py-4"><div className="flex items-center gap-2"><Laptop className="size-5 text-muted" /><h2 className="text-base font-semibold">{t('使用命令行', 'Use the command line')}</h2></div>
        <p className="mt-2 text-sm leading-6 text-muted">{t('安装 CLI，再运行 vibe remote 配对。', 'Install the CLI, then pair with vibe remote.')}</p>
        <Button variant="ghost" className="mt-2 min-h-11 w-full justify-between px-0 text-sm">{t('查看安装与配对步骤', 'See installation and pairing steps')}<ChevronRight /></Button>
      </div>
    </>}
    <div className="mt-auto pt-6"><Secondary><RefreshCw />{t('我已完成配对，刷新', 'I have paired · Refresh')}</Secondary></div>
  </Screen>;
}
export function AccountPage({ account, instance }: { account: MobileAccount; instance?: MobileInstance }) {
  const t = useCopy();
  return <Screen header={<Header title={t('账户', 'Account')} back />}>
    <div className="flex items-center gap-3 py-2"><span className="grid size-14 shrink-0 place-items-center rounded-full bg-muted-soft"><UserRound className="size-6" /></span><div className="min-w-0"><h1 className="text-xl font-semibold">{account.name}</h1><p className="mt-1 break-all text-sm text-muted">{account.email}</p></div></div>
    <h2 className="mb-3 mt-8 text-sm font-semibold">{t('当前实例', 'Current instance')}</h2>
    {instance ? <InstanceRow instance={instance} current /> : <p className="rounded-xl border border-border bg-surface p-4 text-sm text-muted">{t('尚未选择实例', 'No instance selected')}</p>}
    <Button variant="outline" className="mt-3 min-h-12 w-full justify-between text-sm"><span>{instance ? t('切换实例', 'Switch instance') : t('选择实例', 'Choose an instance')}</span><ChevronRight className="size-4" /></Button>
    <Button variant="outline" className="mt-8 min-h-12 w-full justify-start text-sm"><LogOut className="size-4" />{t('退出登录', 'Sign out')}</Button>
    <div className="mt-auto border-t border-border pt-4">
      <Button variant="ghost" className="min-h-12 w-full justify-between px-0 text-sm text-destructive-ink">{t('删除账号', 'Delete account')}<ExternalLink className="size-4" /></Button>
      <p className="text-xs leading-5 text-muted">{t('前往 avibe.bot，查看影响并确认删除。', 'Continue to avibe.bot to review the impact and confirm deletion.')}</p>
    </div>
  </Screen>;
}
export function NativeInstanceError({ instance }: { instance: MobileInstance }) {
  const t = useCopy();
  return <Screen header={<Header />}><div className="flex flex-1 flex-col justify-center py-8">
    <WifiOff className="mb-6 size-12 text-muted" />
    <Heading title={t('暂时无法打开实例', 'Could not open this instance')} body={t('检查网络，或确认运行 Avibe 的电脑已联网。', 'Check your connection and that the computer running Avibe is online.')} />
    <p className="mt-6 break-words text-base font-semibold">{instance.name}</p><p className="mt-1 break-all text-sm text-muted">{instance.host}</p>
    <div className="mt-8 space-y-3"><Primary><RefreshCw />{t('重试', 'Retry')}</Primary><Secondary>{t('切换实例', 'Switch instance')}</Secondary></div>
  </div></Screen>;
}
export function NativeWindowTopBar() {
  const t = useCopy();
  return <Screen header={<header className="shrink-0 border-b border-border bg-surface px-4 pb-3">
    <div className="flex min-h-14 items-center justify-between gap-2"><Button variant="ghost" className="min-h-11 px-1 text-base">{t('完成', 'Done')}</Button><Button variant="ghost" className="min-h-11 px-1 text-sm">{t('在 Safari 中打开', 'Open in Safari')}<ExternalLink className="size-4" /></Button></div>
    <div className="flex items-center justify-center gap-2 rounded-lg bg-muted-soft px-3 py-2 text-sm"><ShieldCheck className="size-4 shrink-0 text-muted" /><span className="truncate">docs.avibe.bot</span></div>
  </header>}>
    <div className="pt-5"><p className="text-sm font-semibold">Avibe</p><h1 className="mt-6 text-2xl font-semibold">{t('开始使用', 'Get started')}</h1><p className="mt-4 text-base leading-7 text-muted">{t('在你的电脑上运行 Avibe，随时连接你的 AI 伙伴。', 'Run Avibe on your computer and reach your AI partners from anywhere.')}</p></div>
  </Screen>;
}
