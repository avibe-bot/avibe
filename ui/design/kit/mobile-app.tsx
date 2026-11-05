import { createContext, useContext, type ReactNode } from 'react';
import {
  AlertTriangle, ArrowDown, ArrowLeft, Check, ChevronRight,
  ExternalLink, Globe, Laptop, Loader2, MonitorDown, RotateCcw, RotateCw, UserRound,
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { BotAvatar } from '@/components/ui/bot-avatar';
import { WorkbenchPageHeader } from '@/components/workbench/WorkbenchPageHeader';
import { PageSectionTitle } from '@/components/workbench/PageSectionTitle';
import { MoreAccountSection } from '@/components/workbench/MorePage';
import { MobileAppHeader } from '@/components/apps/MobileAppHeader';
import { PhoneScreen } from '../shell/PhoneScreen';

export type MobileLocale = 'zh' | 'en';
export interface MobileLoginFixture {
  step: 'email' | 'code';
  email: string;
  code?: string;
  busy?: 'sending' | 'resending' | 'verifying';
  error?: 'email-invalid' | 'code-invalid' | 'code-expired' | 'rate-limited' | 'network' | 'resend-failed';
  cooldown?: 60 | 15;
  resent?: boolean;
}
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

/** AppShell's shipped MobileBrandHeader and phone insets. Only the instance-only tab bar is absent. */
function Screen({ children, header }: { children: ReactNode; header?: ReactNode }) {
  return <div className="h-full [&>div>main]:pb-0"><PhoneScreen header={header} tabBar={<></>}>
    <div className="mx-auto flex max-w-xl flex-col gap-6">{children}</div>
  </PhoneScreen></div>;
}
function AccountAction() {
  const t = useCopy();
  return <Button variant="ghost" size="icon" className="size-8 text-muted" aria-label={t('账户', 'Account')}><UserRound className="size-4" /></Button>;
}
function BackAction() {
  const t = useCopy();
  return <Button variant="ghost" size="sm" className="self-start" ><ArrowLeft />{t('返回', 'Back')}</Button>;
}

export function WelcomeIntro({ fixture }: { fixture: MobileLoginFixture }) {
  const t = useCopy();
  const { step, email, code = '', busy, error, cooldown, resent } = fixture;
  const codeStep = step === 'code';
  const fieldInvalid = error === 'email-invalid' || error === 'code-invalid' || error === 'code-expired';
  const notice = error === 'email-invalid' ? t('请填写有效的邮箱以接收验证码。', 'Enter a valid email to receive a verification code.')
    : error === 'code-invalid' || error === 'code-expired' ? t('验证码无效或已过期，请重试或重新发送邮件。', 'Invalid or expired code. Try again or resend the email.')
    : error === 'rate-limited' ? t('请求过于频繁，请稍后再试。', 'Too many requests. Please try again later.')
    : error === 'network' ? t('暂时连不上 avibe.bot。检查网络后重试。', 'Could not reach avibe.bot. Check your connection and retry.')
    : error === 'resend-failed' ? t('发送失败，请稍后再试。', "Couldn't resend. Try again in a moment.") : null;
  return <Screen>
    <WorkbenchPageHeader icon={<Globe className="size-5" />} title={t('登录 / 注册', 'Sign in / Create account')} subtitle={codeStep ? t('输入邮件里的 6 位验证码', 'Enter the 6-digit code from your email') : t('在手机上访问你的 Agent OS', 'Your Agent OS, on your phone')} />
    {!codeStep && <div className="overflow-hidden rounded-xl border border-border bg-surface">
      <div className="flex items-center gap-3 px-4 py-3">
        <BotAvatar avatar={{ kind: 'mascot' }} name="Vibey" size={40} />
        <div className="min-w-0"><p className="text-sm font-semibold">Vibey</p><p className="text-[12px] text-muted">{t('内置的 AI 伙伴，随时在身边', 'Your built-in AI partner, wherever you are')}</p></div>
      </div>
      <p className="border-t border-border px-4 py-3 text-sm text-muted">{t('登录 avibe.bot，选择自己的实例或别人共享的实例。', 'Sign in to avibe.bot, then choose your own instance or one shared with you.')}</p>
    </div>}
    {codeStep && <div className="flex flex-col gap-2">
      <p role="status" className="break-all text-sm text-muted">{t('验证码已发送至', 'Code sent to')} <span className="font-medium text-foreground">{email}</span></p>
      <Button variant="text" size="sm" className="self-start" disabled={Boolean(busy)}>{t('修改邮箱', 'Change email')}</Button>
    </div>}
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2">
        <Label htmlFor={codeStep ? 'mobile-login-code' : 'mobile-login-email'}>{codeStep ? t('验证码', 'Verification code') : t('邮箱', 'Email')}</Label>
        {codeStep ? <Input id="mobile-login-code" type="text" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} defaultValue={code} placeholder="000000" disabled={Boolean(busy)} aria-invalid={fieldInvalid || undefined} aria-describedby={`mobile-login-hint${notice ? ' mobile-login-error' : ''}`} />
          : <Input id="mobile-login-email" type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false} defaultValue={email} placeholder="you@example.com" disabled={Boolean(busy)} aria-invalid={fieldInvalid || undefined} aria-describedby={`mobile-login-hint${notice ? ' mobile-login-error' : ''}`} />}
        <p id="mobile-login-hint" className="text-[12px] leading-relaxed text-muted">{codeStep ? t('支持粘贴验证码。邮件里的链接会打开 Safari，不会回到 App。请在这里输入验证码。', 'You can paste the code. The email link opens Safari and will not return to the App. Enter the code here.')
          : t('新邮箱将自动注册。在 App 内完成登录，无需密码。', 'New emails create an account automatically. Sign in here without a password.')}</p>
        {notice && <p id="mobile-login-error" role="alert" className="text-[12px] leading-relaxed text-destructive-ink">{notice}</p>}
      </div>
      <Button disabled={Boolean(busy) || (codeStep && code.length !== 6)}>
        {busy === 'sending' || busy === 'verifying' ? <Loader2 className="animate-spin" /> : null}
        {codeStep ? (busy === 'verifying' ? t('验证中…', 'Verifying…') : t('验证并登录', 'Verify and sign in')) : (busy === 'sending' ? t('发送中…', 'Sending…') : t('发送验证码', 'Send verification code'))}
      </Button>
      {codeStep && <>
        <Button variant="outline" disabled={Boolean(busy) || Boolean(cooldown)}>
          {busy === 'resending' ? <Loader2 className="animate-spin" /> : null}
          {busy === 'resending' ? t('重新发送中…', 'Resending…') : cooldown ? t(`${cooldown} 秒后可重发`, `Resend in ${cooldown}s`) : t('重新发送', 'Resend email')}
        </Button>
        {resent && <p role="status" className="text-[12px] text-muted">{t('已重新发送，请再次查看邮箱。', 'Sent — check your inbox again.')}</p>}
        <p className="text-[12px] leading-relaxed text-muted">{t('等了一分钟还没收到？检查垃圾邮件文件夹，重新发送，或者换一个邮箱。', "If it hasn't arrived after a minute, check your spam folder, resend, or try another email.")}</p>
      </>}
    </div>
  </Screen>;
}

/** MobileProjectRow's compact composition; only the instance metadata is new. */
function InstanceRow({ instance, current = false }: { instance: MobileInstance; current?: boolean }) {
  const t = useCopy();
  return <div className="overflow-hidden rounded-xl border border-border bg-surface">
    <button type="button" className="flex w-full min-w-0 items-center gap-2.5 px-4 py-3.5 text-left">
      <Laptop className="size-4 shrink-0 text-muted" />
      <span className="min-w-0 flex-1">
        <span className="block break-words text-sm font-semibold">{instance.name}</span>
        <span className="mt-1 block break-all font-mono text-[11px] text-muted">{instance.host}</span>
        <span className="mt-2 flex flex-wrap items-center gap-1.5">
          <Badge variant={instance.online ? 'success' : 'secondary'}>{instance.online ? t('在线', 'Online') : t('离线', 'Offline')}</Badge>
          {instance.owner && <Badge variant="info">{t('共享', 'Shared')}</Badge>}
          {current && <Badge variant="outline"><Check className="size-3" />{t('当前', 'Current')}</Badge>}
        </span>
        <span className="mt-1 block break-words text-[12px] text-muted">{instance.owner ? t(`${instance.owner} 共享`, `Shared by ${instance.owner}`) : t('我的实例', 'Yours')}</span>
      </span>
      <ChevronRight className="size-4 shrink-0 text-muted" />
    </button>
  </div>;
}
export function InstancePicker({ state, instances, currentId, selectedOffline }: { state: PickerState; instances: MobileInstance[]; currentId?: string; selectedOffline?: MobileInstance }) {
  const t = useCopy();
  const loading = state === 'loading';
  return <Screen>
    <WorkbenchPageHeader icon={<Laptop className="size-5" />} title={currentId ? t('切换实例', 'Switch instance') : t('选择实例', 'Choose an instance')} subtitle={t('自己的实例，以及别人共享的实例', 'Your instances and those shared with you')} actions={<AccountAction />} />
    <div className="flex flex-col gap-2">
      <PageSectionTitle title={t('实例', 'Instances')} action={<Button variant="ghost" size="icon" className="size-8 text-muted" aria-label={t('刷新实例', 'Refresh instances')} disabled={loading || state === 'refreshing'}><RotateCw className="size-4" /></Button>} />
      {(state === 'pulling' || state === 'refreshing') && <div role="status" className="flex items-center justify-center gap-2 px-3 py-3 text-[13px] text-muted">{state === 'pulling' ? <ArrowDown className="size-3.5" /> : <Loader2 className="size-3.5 animate-spin" />}{state === 'pulling' ? t('松开即可刷新', 'Release to refresh') : t('正在刷新…', 'Refreshing…')}</div>}
      {loading ? <div role="status" className="flex items-center justify-center gap-2 rounded-xl border border-border bg-surface px-4 py-10 text-sm text-muted"><Loader2 className="size-4 animate-spin" />{t('正在加载实例…', 'Loading instances…')}</div>
        : state === 'error' ? <button type="button" className="flex items-center justify-center gap-2 rounded-xl border border-border bg-surface px-4 py-10 text-sm text-muted transition hover:text-foreground"><RotateCw className="size-4" />{t('无法加载实例，点击重试', 'Could not load instances. Tap to retry.')}</button>
        : instances.map(instance => <InstanceRow key={instance.id} instance={instance} current={instance.id === currentId} />)}
    </div>
    {state === 'offline-tapped' && selectedOffline && <div role="status" className="rounded-xl border border-border bg-surface px-4 py-3">
      <p className="break-words text-sm font-semibold">{t(`${selectedOffline.name} 处于离线状态`, `${selectedOffline.name} is offline`)}</p>
      <p className="mt-2 text-[12px] leading-relaxed text-muted">{t('在这台电脑上启动 Avibe 并连接网络，然后用列表上方的刷新按钮更新状态，或选择其他在线实例。', 'Start Avibe on that computer and connect it to the internet. Refresh from the list header, or choose another online instance.')}</p>
    </div>}
    {!loading && state !== 'error' && <Button variant="text" size="sm" className="self-start">{t('添加自己的实例', 'Add your own instance')}<ChevronRight /></Button>}
  </Screen>;
}

export function EmptyInstancesPage({ cli = false }: { cli?: boolean }) {
  const t = useCopy();
  return <Screen>
    {cli && <BackAction />}
    <WorkbenchPageHeader icon={<Laptop className="size-5" />} title={cli ? t('安装与配对', 'Install and pair') : t('选择实例', 'Choose an instance')} subtitle={t('在电脑上运行，从手机上连接', 'Run on your computer. Connect from your phone.')} actions={!cli && <AccountAction />} />
    {cli ? <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">{t('在 macOS 或 Linux 终端运行。Windows 用户请按指南使用 WSL。', 'Run in Terminal on macOS or Linux. On Windows, follow the guide for WSL.')}</p>
      <PageSectionTitle title={t('1. 安装并完成设置', '1. Install and finish setup')} />
      <code className="break-all rounded-lg border border-border bg-surface-2 px-3 py-3 font-mono text-[12px] leading-relaxed">bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --launch'</code>
      <PageSectionTitle title={t('2. 配对你的账号', '2. Pair your account')} />
      <code className="rounded-lg border border-border bg-surface-2 px-3 py-3 font-mono text-[12px]">vibe remote</code>
      <p className="text-[12px] leading-relaxed text-muted">{t('按终端提示登录相同的 avibe.bot 账号，完成配对后回到手机刷新。', 'Sign in with the same avibe.bot account when prompted. After pairing, return here and refresh.')}</p>
      <Button variant="outline-cyan" size="sm" className="self-start">{t('安装指南', 'Installation guide')}<ExternalLink /></Button>
    </div> : <>
      <div className="rounded-xl border border-border bg-surface px-4 py-10 text-center text-sm text-muted">{t('还没有可用实例。先在电脑上安装 Avibe，并配对这个账号。', 'No instances yet. Install Avibe on your computer and pair this account.')}</div>
      <div className="flex flex-col gap-2"><PageSectionTitle title={t('开始使用', 'Get started')} />
        <div className="overflow-hidden rounded-xl border border-border bg-surface">
          <div className="flex items-center gap-3 px-4 py-3"><MonitorDown className="size-4 shrink-0 text-muted" /><div className="min-w-0 flex-1"><p className="text-sm font-medium">{t('下载桌面版', 'Download the desktop app')}</p><p className="mt-1 text-[12px] text-muted">{t('完成设置后，在「远程访问」中配对', 'Finish setup, then pair in Remote Access')}</p></div><ExternalLink className="size-4 text-muted" /></div>
          <div className="flex items-center gap-3 border-t border-border px-4 py-3"><Laptop className="size-4 shrink-0 text-muted" /><div className="min-w-0 flex-1"><p className="text-sm font-medium">{t('使用命令行', 'Use the command line')}</p><p className="mt-1 text-[12px] text-muted">{t('安装 CLI，再运行 vibe remote', 'Install the CLI, then run vibe remote')}</p></div><ChevronRight className="size-4 text-muted" /></div>
        </div>
      </div>
    </>}
    <Button variant="outline" size="sm" className="self-start"><RotateCw />{t('我已完成配对，刷新', 'I have paired · Refresh')}</Button>
  </Screen>;
}

export function AccountPage({ instance }: { instance?: MobileInstance }) {
  const t = useCopy();
  return <Screen><BackAction />
    <WorkbenchPageHeader icon={<UserRound className="size-5" />} title={t('账户', 'Account')} />
    <MoreAccountSection />
    <div className="flex flex-col gap-2"><PageSectionTitle title={t('当前实例', 'Current instance')} />
      {instance ? <InstanceRow instance={instance} current /> : <div className="rounded-xl border border-border bg-surface px-4 py-10 text-center text-sm text-muted">{t('尚未选择实例', 'No instance selected')}</div>}
      <Button variant="outline" size="sm" className="self-start">{instance ? t('切换实例', 'Switch instance') : t('选择实例', 'Choose an instance')}<ChevronRight /></Button>
    </div>
    <div className="border-t border-border pt-3"><Button variant="ghost" size="sm" className="text-destructive-ink">{t('删除账号', 'Delete account')}<ExternalLink /></Button><p className="mt-1 text-[12px] text-muted">{t('前往 avibe.bot，查看影响并确认删除。', 'Continue to avibe.bot to review the impact and confirm deletion.')}</p></div>
  </Screen>;
}

/** ErrorFallback's existing layout, with instance recovery actions in place of app reload. */
export function NativeInstanceError({ instance }: { instance: MobileInstance }) {
  const t = useCopy();
  return <Screen><div className="grid min-h-[60vh] w-full place-items-center bg-surface p-6 text-center"><div className="flex max-w-sm flex-col items-center gap-3">
    <span className="grid size-12 shrink-0 place-items-center rounded-2xl border border-gold/40 bg-gold/[0.08]"><AlertTriangle className="size-6 text-gold-ink" /></span>
    <div className="text-[15px] font-semibold text-foreground">{t('暂时无法打开实例', 'Could not open this instance')}</div>
    <div className="text-[12.5px] text-muted">{t('检查网络，或确认运行 Avibe 的电脑已联网。', 'Check your connection and that the computer running Avibe is online.')}</div>
    <div className="max-w-full break-all rounded bg-surface-3 px-2 py-1 font-mono text-[11px] text-muted">{instance.name}<br />{instance.host}</div>
    <div className="mt-1 flex flex-wrap items-center justify-center gap-2"><Button size="sm" variant="brand" className="gap-1.5"><RotateCcw />{t('重试', 'Retry')}</Button><Button size="sm" variant="outline" className="gap-1.5">{t('切换实例', 'Switch instance')}</Button></div>
  </div></div></Screen>;
}
export function NativeWindowTopBar() {
  const t = useCopy();
  return <Screen header={<MobileAppHeader title="docs.avibe.bot" icon={Globe} actions={<>
    <Button variant="ghost" size="compact">{t('完成', 'Done')}</Button>
    <Button variant="ghost" size="compact">{t('在 Safari 中打开', 'Open in Safari')}</Button>
  </>} />}>
    <WorkbenchPageHeader icon={<Globe className="size-5" />} title={t('开始使用', 'Get started')} subtitle="Avibe" />
    <p className="text-sm text-muted">{t('在电脑上运行 Avibe，连接你的 AI 伙伴。', 'Run Avibe on your computer and connect to your AI partner.')}</p>
  </Screen>;
}
