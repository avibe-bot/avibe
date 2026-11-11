import { createContext, useContext, type ReactNode } from 'react';
import { AlertTriangle, ArrowDown, ArrowLeft, ArrowRight, Check, ChevronRight, ExternalLink, Eye, EyeOff, Globe, KeyRound, Laptop, Mail, MonitorDown, Plus, RotateCw, ScrollText, ShieldCheck, Sparkles, UserRound } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { MoreAccountSection } from '@/components/workbench/MorePage';
import { MobileAppHeader } from '@/components/apps/MobileAppHeader';
import logo from '@/assets/logo.png';
import mailMascot from '@/assets/pet/vibey-message.png';
import heroMascot from '../../../assets/mascot/cloud-tuanzi-v2.png';
import { cn } from '@/lib/utils';
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
  compact?: boolean;
  /** `password/*`: the email step also offers password sign-in. */
  password?: boolean;
}
/** The second step after the email code, for an account with two-step verification. */
export interface MobileSecondFactorFixture {
  email: string;
  methods: ('passkey' | 'totp')[];
  step: 'passkey' | 'totp' | 'recovery' | 'setup';
  code?: string;
  busy?: 'passkey' | 'verifying';
  error?: 'passkey-failed' | 'code-invalid' | 'rate-limited' | 'network';
  compact?: boolean;
}
/**
 * Password sign-in, the alternative first factor for an account that set a password on avibe.bot:
 * the email step's password mode, one form with the email and the password.
 */
export interface MobilePasswordFixture {
  email: string;
  password?: string;
  shown?: boolean;
  /** The field with focus (drawn statically). */
  focused?: 'email' | 'password';
  busy?: boolean;
  error?: 'email-invalid' | 'invalid-credentials' | 'email-not-confirmed' | 'rate-limited' | 'network';
  /** Seconds before Sign in is offered again after `rate-limited`; static here. */
  wait?: number;
  /**
   * The region above the keyboard on a small phone: the page has scrolled the
   * illustration and title away, so the form and its Sign in stay in view.
   */
  compact?: boolean;
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

/** Selected A world. Static composition, not application navigation. */
function CloudScreen({ children, header }: { children: ReactNode; header?: ReactNode }) {
  return <div className="h-full [&>div>main]:pb-0"><PhoneScreen header={header ?? <></>} tabBar={<></>} bare flat>
    <div className="relative flex min-h-0 flex-1 flex-col overflow-y-auto" style={{ background: 'var(--gradient-wizard-platforms)' }}>
      <div className="relative mx-auto flex min-h-full w-full max-w-xl flex-col p-6">{children}</div>
    </div>
  </PhoneScreen></div>;
}
function Brand({ back = false, account = false }: { back?: boolean; account?: boolean }) {
  const t = useCopy();
  return <header className="flex h-11 shrink-0 items-center justify-between">
    <div className="flex items-center gap-2.5">{back ? <Button variant="ghost" size="icon" aria-label={t('返回', 'Back')}><ArrowLeft className="size-5" /></Button> : <img src={logo} alt="" className="size-8" />}<span className="text-lg font-semibold tracking-tight">Avibe</span></div>
    {account ? <Button variant="ghost" size="icon" className="rounded-full border border-edge bg-field" aria-label={t('账户', 'Account')}><UserRound className="size-4" /></Button> : <span className="text-xs text-muted">Agent OS</span>}
  </header>;
}
function Primary({ children, disabled = false }: { children: ReactNode; disabled?: boolean }) {
  return <Button variant="brand" size="lg" disabled={disabled} className="w-full gap-3 rounded-full">{children}<ArrowRight className="size-4" /></Button>;
}
const raised = 'rounded-3xl border border-edge bg-surface/80 p-5 shadow-elevation-0';
/** A secondary way through a step, below its primary action. */
const alternative = 'h-auto max-w-full whitespace-normal px-0 text-sm text-foreground underline';
function PageTitle({ title, subtitle }: { title: string; subtitle?: string }) {
  return <div className="pt-9 pb-7"><h1 className="text-3xl font-semibold leading-tight tracking-tight">{title}</h1>{subtitle && <p className="mt-4 text-sm leading-6 text-muted">{subtitle}</p>}</div>;
}
export function WelcomePage() {
  const t = useCopy();
  return <CloudScreen><Brand /><div className="relative flex flex-1 flex-col items-center justify-center py-8">
    <div className="relative mb-8 grid size-64 place-items-center"><div className="absolute inset-0 rounded-full border border-edge bg-surface/30 shadow-elevation-0" /><div className="absolute inset-6 rounded-full border border-surface/70 bg-surface/30" /><img src={heroMascot} alt={t('Vibey 团子', 'Vibey cloud mascot')} className="relative size-52 object-contain" /><span className="absolute -right-1 top-9 grid size-12 rotate-12 place-items-center rounded-2xl border border-edge bg-surface shadow-elevation-0"><Sparkles className="size-5 text-mint-ink" /></span><span className="absolute -left-1 bottom-5 rounded-full border border-edge bg-surface px-4 py-2 text-sm shadow-elevation-0">{t('你好，我是 Vibey', 'Hi, I’m Vibey')}</span></div>
    <h1 className="text-center text-4xl font-semibold leading-tight tracking-tight">{t('你的 AI 伙伴，', 'Your AI partner,')}<br />{t('随身同行。', 'by your side.')}</h1><p className="mt-5 max-w-xs text-center text-sm leading-7 text-muted">{t('电脑上的 Avibe，', 'Avibe on your computer,')}<br />{t('现在也在你的口袋里。', 'now in your pocket.')}</p>
  </div><div className="flex flex-col gap-4 pb-3"><Primary>{t('登录 / 注册', 'Sign in / Create account')}</Primary><p className="text-center text-xs leading-relaxed text-muted">{t('Vibey，你的内置 AI 伙伴。', 'Vibey, your built-in AI partner.')}<br />{t('连接自己的实例，也可以加入别人共享的实例。', 'Connect to your instances or those shared with you.')}</p></div></CloudScreen>;
}

/** One accessible input; six equally spaced cells are a decorative fixture display. */
function CodeInput({ code, invalid, busy, label }: { code: string; invalid: boolean; busy: boolean; label?: string }) {
  const t = useCopy();
  return <div className="relative"><Label htmlFor="mobile-login-code" className="sr-only">{label ?? t('输入邮件里的 6 位验证码', 'Enter the 6-digit code from your email')}</Label>
    <Input id="mobile-login-code" className="sr-only" type="text" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} defaultValue={code} disabled={busy} aria-invalid={invalid || undefined} aria-describedby="mobile-login-hint mobile-login-feedback" />
    <div aria-hidden="true" className="grid grid-cols-6 gap-2">{Array.from({ length: 6 }, (_, index) => <span key={index} className={cn('grid aspect-square min-w-0 place-items-center rounded-2xl border bg-surface text-2xl font-medium tabular-nums shadow-elevation-0', invalid ? 'border-destructive text-destructive-ink' : 'border-edge', !invalid && !busy && index === code.length && 'border-mint ring-2 ring-mint/30')}>{code[index] || (!busy && index === code.length ? <span className="h-6 w-px bg-mint-ink" /> : null)}</span>)}</div>
  </div>;
}
export function WelcomeIntro({ fixture }: { fixture: MobileLoginFixture }) {
  const t = useCopy();
  const { step, email, code = '', busy, error, cooldown, resent, compact = false } = fixture;
  const codeStep = step === 'code';
  const invalid = error === 'email-invalid' || error === 'code-invalid' || error === 'code-expired';
  const notice = error === 'email-invalid' ? (fixture.password ? t('请填写有效的邮箱。', 'Enter a valid email address.') : t('请填写有效的邮箱以接收验证码。', 'Enter a valid email to receive a verification code.'))
    : error === 'code-invalid' || error === 'code-expired' ? t('验证码无效或已过期，请重试或重新发送邮件。', 'Invalid or expired code. Try again or resend the email.')
      : error === 'rate-limited' ? t('请求过于频繁，请稍后再试。', 'Too many requests. Please try again later.')
        : error === 'network' ? t('暂时连不上 avibe.bot。检查网络后重试。', 'Could not reach avibe.bot. Check your connection and retry.')
          : error === 'resend-failed' ? t('发送失败，请稍后再试。', 'Couldn’t resend. Please try again later.') : null;
  return <CloudScreen><Brand back /><div className={cn('flex flex-col items-center', compact ? 'pt-4 pb-4' : 'pt-8 pb-6')}><img src={mailMascot} alt="" className={cn('object-contain', compact ? 'mb-3 size-16' : 'mb-5 size-28')} /><h1 className={cn('text-center font-semibold leading-tight tracking-tight', compact ? 'text-2xl' : 'text-3xl')}>{codeStep ? t('查看你的邮箱。', 'Check your inbox.') : t('从一封邮件开始。', 'It starts with an email.')}</h1>{!codeStep && <p className="mt-3 text-center text-sm leading-6 text-muted">{t('登录 / 注册你的 avibe.bot 账号', 'Sign in or create your avibe.bot account')}</p>}</div>
    {codeStep ? <>
      <div className={cn('text-center', compact ? 'pb-5' : 'pb-7')}><p className="text-sm leading-7 text-muted">{t('验证码已发送至', 'Code sent to')}</p><p className="break-all text-sm font-medium leading-6">{email}</p><Button variant="link" size="compact" disabled={Boolean(busy)} className="mt-1 px-0 text-muted underline">{t('修改邮箱', 'Change email')}</Button></div>
      <CodeInput code={code} invalid={invalid} busy={Boolean(busy)} /><p id="mobile-login-hint" className="mt-5 text-center text-sm leading-6 text-muted">{t('输入邮件里的 6 位验证码', 'Enter the 6-digit code from your email')}</p>
      <div id="mobile-login-feedback" className="mt-3 min-h-6 text-center text-xs leading-6">{notice ? <p role="alert" className="text-destructive-ink">{notice}</p> : resent ? <p role="status" className="text-mint-ink">{t('已重新发送，请再次查看邮箱。', 'Sent — check your inbox again.')}</p> : null}</div>
      <div className="mt-5" aria-busy={busy === 'verifying'}><Primary disabled={Boolean(busy) || code.length !== 6}>{busy === 'verifying' ? t('验证中…', 'Verifying…') : t('验证并登录', 'Verify and sign in')}</Primary></div>
      <div className="mt-6 text-center text-sm leading-7"><p className="text-muted">{t('没收到验证码？', 'Didn’t receive a code?')}</p><Button variant="link" size="compact" disabled={Boolean(busy) || Boolean(cooldown)} className="h-auto max-w-full whitespace-normal px-0 text-sm text-foreground underline">{busy === 'resending' ? t('重新发送中…', 'Resending…') : cooldown ? t(`${cooldown} 秒后可重新发送`, `Resend in ${cooldown}s`) : t('重新发送', 'Resend')}</Button></div>
      <p className="mt-auto pt-7 pb-3 text-center text-xs leading-6 text-muted">{t('可直接粘贴验证码，也可以检查垃圾邮件文件夹。', 'Paste your code here. You can also check your spam folder.')}</p>
    </> : <>
      <div className="rounded-3xl border border-edge bg-surface/70 p-6 shadow-elevation-0"><div className="flex flex-col gap-6"><div className="flex flex-col gap-3"><Label htmlFor="mobile-login-email">{t('邮箱', 'Email')}</Label><Input id="mobile-login-email" type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false} defaultValue={email} placeholder="you@example.com" disabled={Boolean(busy)} aria-invalid={invalid || undefined} aria-describedby="mobile-login-hint mobile-login-feedback" className="h-14 rounded-2xl px-4 text-base" /><p id="mobile-login-hint" className="text-xs leading-6 text-muted">{t('新邮箱将自动注册，无需设置密码。', 'New emails create an account automatically. No password needed.')}</p><div id="mobile-login-feedback">{notice && <p role="alert" className="text-xs leading-6 text-destructive-ink">{notice}</p>}</div></div><div className="flex flex-col gap-3"><div aria-busy={busy === 'sending'}><Primary disabled={Boolean(busy)}>{busy === 'sending' ? t('发送中…', 'Sending…') : t('发送验证码', 'Send verification code')}</Primary></div>{fixture.password && <Button variant="link" size="compact" disabled={Boolean(busy)} className={cn(alternative, 'gap-2 self-center')}><KeyRound className="size-4" />{t('使用密码登录', 'Sign in with password')}</Button>}</div></div></div>
      <p className="mt-auto pt-8 pb-4 text-center text-xs leading-6 text-muted">{t('验证码会送到你的邮箱，在这里完成登录。', 'We’ll email you a code. Enter it here to sign in.')}</p>
    </>}
  </CloudScreen>;
}
function InstanceRow({ instance, current = false }: { instance: MobileInstance; current?: boolean }) {
  const t = useCopy();
  return <button type="button" className={cn(raised, 'w-full text-left', current && 'border-mint/50')}><span className="flex items-start gap-3"><span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-mint-soft"><Laptop className={cn('size-5', instance.online ? 'text-mint-ink' : 'text-muted')} /></span><span className="min-w-0 flex-1"><span className="block break-words text-base font-semibold">{instance.name}</span><span className="mt-1 block break-all text-xs leading-5 text-muted">{instance.host}</span><span className="mt-3 flex flex-wrap items-center gap-2"><Badge variant={instance.online ? 'success' : 'secondary'}>{instance.online ? t('在线', 'Online') : t('离线', 'Offline')}</Badge>{instance.owner && <Badge variant="info">{t('共享', 'Shared')}</Badge>}{current && <Badge variant="outline"><Check className="size-3" />{t('当前', 'Current')}</Badge>}</span><span className="mt-2 block break-words text-xs leading-5 text-muted">{instance.owner ? t(`${instance.owner} 共享`, `Shared by ${instance.owner}`) : t('我的实例', 'Yours')}</span></span><ChevronRight className="mt-3 size-4 shrink-0 text-muted" /></span></button>;
}
/** Still skeleton; the approved shimmer and hand-over are specified in the handoff. */
function InstanceSkeleton() {
  return <div aria-hidden="true" className={cn(raised, 'flex gap-3')}><Skeleton className="size-11 rounded-2xl" /><div className="min-w-0 flex-1"><Skeleton className="h-6 w-3/4" /><Skeleton className="mt-1 h-5 w-full" /><div className="mt-3 h-6"><Skeleton className="h-full w-1/3 rounded-full" /></div><Skeleton className="mt-2 h-5 w-1/2" /></div></div>;
}
export function InstancePicker({ state, instances, currentId, selectedOffline }: { state: PickerState; instances: MobileInstance[]; currentId?: string; selectedOffline?: MobileInstance }) {
  const t = useCopy();
  const loading = state === 'loading';
  return <CloudScreen><Brand account /><div className="flex items-start justify-between gap-3"><PageTitle title={currentId ? t('切换实例', 'Switch instance') : t('想从哪里开始？', 'Where shall we begin?')} subtitle={t('自己的实例，以及别人共享的实例', 'Your instances and those shared with you')} /><Button variant="ghost" size="icon" className="mt-9 shrink-0 rounded-full" disabled={loading || state === 'refreshing'} aria-label={t('刷新实例', 'Refresh instances')}><RotateCw className="size-4" /></Button></div>
    {(state === 'pulling' || state === 'refreshing') && <p role="status" className="mb-4 flex items-center justify-center gap-2 text-sm text-muted">{state === 'pulling' && <ArrowDown className="size-4" />}{state === 'pulling' ? t('松开即可刷新', 'Release to refresh') : t('正在刷新…', 'Refreshing…')}</p>}
    <div className="flex flex-col gap-4" aria-busy={loading || state === 'refreshing'}>{loading ? <><span role="status" className="sr-only">{t('正在加载实例…', 'Loading instances…')}</span><InstanceSkeleton /><InstanceSkeleton /></> : state === 'error' ? <div className={cn(raised, 'py-8 text-center')}><AlertTriangle className="mx-auto mb-4 size-7 text-gold-ink" /><p className="text-base font-semibold">{t('暂时无法加载实例', 'Couldn’t load instances')}</p><p className="mt-3 text-sm leading-6 text-muted">{t('检查网络后，再试一次。', 'Check your connection and try again.')}</p><Button variant="outline" className="mt-5 rounded-full"><RotateCw className="size-4" />{t('重试', 'Retry')}</Button></div> : instances.map(instance => <InstanceRow key={instance.id} instance={instance} current={instance.id === currentId} />)}</div>
    {state === 'offline-tapped' && selectedOffline && <div role="status" className={cn(raised, 'mt-4')}><p className="break-words text-sm font-semibold">{t(`${selectedOffline.name} 处于离线状态`, `${selectedOffline.name} is offline`)}</p><p className="mt-2 text-xs leading-6 text-muted">{t('在这台电脑上启动 Avibe 并连接网络，然后刷新列表，或选择其他在线实例。', 'Start Avibe on that computer and connect it to the internet. Then refresh the list or choose another online instance.')}</p></div>}
    {!loading && state !== 'error' && <div className="mt-auto pt-8 pb-3"><Button variant="outline" size="lg" className="w-full gap-2 rounded-full"><Plus className="size-4" />{t('添加自己的实例', 'Add your own instance')}</Button></div>}
  </CloudScreen>;
}
export function EmptyInstancesPage({ cli = false }: { cli?: boolean }) {
  const t = useCopy();
  return <CloudScreen><Brand back={cli} account={!cli} /><div className="pt-7"><span className="grid size-16 place-items-center rounded-3xl border border-edge bg-surface/80 shadow-elevation-0"><Laptop className="size-7 text-mint-ink" /></span></div><PageTitle title={cli ? t('安装与配对', 'Install and pair') : t('让电脑与手机相连。', 'Bring your work along.')} subtitle={t('在电脑上运行，从手机上连接', 'Run on your computer. Connect from your phone.')} />
    {cli ? <div className={cn(raised, 'flex flex-col gap-4')}><p className="text-sm leading-6 text-muted">{t('在 macOS 或 Linux 终端运行。Windows 用户请按指南使用 WSL。', 'Run in Terminal on macOS or Linux. On Windows, follow the guide for WSL.')}</p><h2 className="text-sm font-semibold">{t('1. 安装并完成设置', '1. Install and finish setup')}</h2><code className="break-all rounded-xl bg-surface-2 p-3 font-mono text-xs leading-6">bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --launch'</code><h2 className="text-sm font-semibold">{t('2. 配对你的账号', '2. Pair your account')}</h2><code className="rounded-xl bg-surface-2 p-3 font-mono text-xs">vibe remote</code><p className="text-xs leading-6 text-muted">{t('按终端提示登录相同的 avibe.bot 账号，完成配对后回到手机刷新。', 'Sign in with the same avibe.bot account when prompted. After pairing, return here and refresh.')}</p><Button variant="link" className="self-start px-0">{t('安装指南', 'Installation guide')}<ExternalLink className="size-4" /></Button></div> : <><p className="mb-6 text-sm leading-7 text-muted">{t('还没有可用实例。先在电脑上安装 Avibe，并配对这个账号。', 'No instances yet. Install Avibe on your computer and pair this account.')}</p><div className="flex flex-col gap-4">{[{ icon: MonitorDown, title: t('下载桌面版', 'Download the desktop app'), body: t('完成设置后，在「远程访问」中配对', 'Finish setup, then pair in Remote Access'), external: true }, { icon: Laptop, title: t('使用命令行', 'Use the command line'), body: t('安装 CLI，再运行 vibe remote', 'Install the CLI, then run vibe remote'), external: false }].map(({ icon: Icon, title, body, external }) => <button type="button" key={title} className={cn(raised, 'flex items-start gap-3 text-left')}><span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-mint-soft"><Icon className="size-5 text-mint-ink" /></span><span className="min-w-0 flex-1"><span className="block text-sm font-semibold">{title}</span><span className="mt-2 block text-xs leading-6 text-muted">{body}</span></span>{external ? <ExternalLink className="mt-2 size-4 shrink-0 text-muted" /> : <ChevronRight className="mt-2 size-4 shrink-0 text-muted" />}</button>)}</div></>}
    <div className="mt-auto pt-7 pb-3"><Button variant="outline" size="lg" className="w-full whitespace-normal rounded-full"><RotateCw className="size-4 shrink-0" />{t('我已完成配对，刷新', 'I have paired · Refresh')}</Button></div>
  </CloudScreen>;
}
export function AccountPage({ instance, licenses = false }: { instance?: MobileInstance; licenses?: boolean }) {
  const t = useCopy();
  return <CloudScreen><Brand back /><PageTitle title={t('账户', 'Account')} /><div className="[&>div]:rounded-3xl [&>div]:shadow-elevation-0 [&_.truncate]:overflow-visible [&_.truncate]:whitespace-normal [&_.truncate]:break-all"><MoreAccountSection /></div><section className="mt-8"><h2 className="mb-4 text-sm font-semibold">{t('当前实例', 'Current instance')}</h2>{instance ? <InstanceRow instance={instance} current /> : <div className={cn(raised, 'py-8 text-center text-sm text-muted')}>{t('尚未选择实例', 'No instance selected')}</div>}<Button variant="outline" size="lg" className="mt-4 w-full justify-between rounded-full">{instance ? t('切换实例', 'Switch instance') : t('选择实例', 'Choose an instance')}<ChevronRight className="size-4" /></Button></section>{licenses && <AboutSection />}<div className="mt-auto pt-9 pb-3"><Button variant="link" className="px-0 text-destructive-ink">{t('删除账号', 'Delete account')}<ExternalLink className="size-4" /></Button><p className="mt-2 text-xs leading-6 text-muted">{t('前往 avibe.bot，查看影响并确认删除。', 'Continue to avibe.bot to review the impact and confirm deletion.')}</p></div></CloudScreen>;
}
/** Where recipients get an MPL-2.0 crate's unmodified covered source. */
export interface MobileSourceCrate { name: string; registry: string }
/**
 * One line of the source offer: a crate, or a family that shares one version and
 * one upstream commit (uniffi and its related crates), so the source link appears once.
 */
export interface MobileSourceOffer { crates: MobileSourceCrate[]; version: string; upstream: string }
/** One distinct license text and every component it covers. Text keeps its hard wraps; the page reflows them. */
export interface MobileLicenseGroup { license: string; components: string[]; text: string; open?: boolean }
export interface MobileLicensesFixture { sources: MobileSourceOffer[]; groups: MobileLicenseGroup[] }

/** Account's About section: the App's own information, not the instance's. Proposed. */
function AboutSection() {
  const t = useCopy();
  return <section className="mt-8"><h2 className="mb-4 text-sm font-semibold">{t('关于', 'About')}</h2><button type="button" className={cn(raised, 'flex w-full items-center gap-3 text-left')}><span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-mint-soft"><ScrollText className="size-5 text-mint-ink" /></span><span className="min-w-0 flex-1 text-base font-semibold">{t('开源许可', 'Open-source licenses')}</span><ChevronRight className="size-4 shrink-0 text-muted" /></button></section>;
}
/** Hard wraps become spaces; a blank line still ends a paragraph. */
function paragraphs(text: string) {
  return text.split(/\n\s*\n/).map(block => block.split('\n').map(line => line.trim()).join(' ').trim()).filter(Boolean);
}
const textLink = 'inline-flex items-center gap-1 text-sm font-medium text-foreground underline underline-offset-4';
/**
 * Open-source licenses, generated from the notices collected with the App
 * binary: the MPL-2.0 source offer first, then each distinct license text once
 * with the components it covers. Proposed.
 */
export function LicensesPage({ fixture }: { fixture: MobileLicensesFixture }) {
  const t = useCopy();
  const total = fixture.groups.reduce((sum, group) => sum + group.components.length, 0);
  return <CloudScreen><Brand back /><PageTitle title={t('开源许可', 'Open-source licenses')} subtitle={t(`Avibe App 包含 ${total} 个开源组件。感谢它们的作者。`, `The Avibe app includes ${total} open-source components. Thanks to their authors.`)} />
    <section className={raised} aria-labelledby="licenses-source"><h2 id="licenses-source" className="text-base font-semibold">{t('MPL-2.0 组件的源代码', 'Source code for MPL-2.0 components')}</h2><p className="mt-2 text-sm leading-6 text-muted">{t('以下组件按 Mozilla Public License 2.0 分发，未作修改。可从 crates.io 归档或上游源代码获取它们的源代码。', 'These components are distributed unmodified under the Mozilla Public License 2.0. Get their source code from the crates.io archive or the upstream source.')}</p>
      <ul className="mt-4 divide-y divide-edge border-t border-edge">{fixture.sources.map(source => {
        const [first, ...related] = source.crates;
        return <li key={first.name} className="py-4 last:pb-0">
          <p className="break-words text-sm font-semibold">{related.length ? t(`${first.name} 及 ${related.length} 个相关组件，${source.version}`, `${first.name} and ${related.length} related crates, ${source.version}`) : <>{first.name} <span className="font-normal text-muted">{source.version}</span></>}</p>
          {related.length > 0 && <p className="mt-1 break-words text-xs leading-5 text-muted">{t('crates.io 归档：', 'crates.io archives: ')}{source.crates.map((crate, index) => <span key={crate.name}>{index > 0 && ', '}<a href={crate.registry} className="underline underline-offset-2">{crate.name}</a></span>)}</p>}
          <p className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">{!related.length && <a href={first.registry} className={textLink}>{t('crates.io 归档', 'crates.io archive')}<ExternalLink className="size-3.5" /></a>}<a href={source.upstream} className={textLink}>{t('源代码', 'Source code')}<ExternalLink className="size-3.5" /></a></p>
        </li>;
      })}</ul>
    </section>
    <section className="mt-8" aria-labelledby="licenses-list"><h2 id="licenses-list" className="mb-4 text-sm font-semibold">{t('许可文本', 'License texts')}</h2>
      <div className="divide-y divide-edge rounded-3xl border border-edge bg-surface/80 shadow-elevation-0">{fixture.groups.map(group => <details key={group.license + group.components[0]} open={group.open} className="group">
        <summary className="flex cursor-pointer list-none items-center gap-3 p-5 [&::-webkit-details-marker]:hidden"><span className="min-w-0 flex-1"><span className="block text-base font-semibold">{group.license}</span><span className="mt-1 block text-xs text-muted">{t(`${group.components.length} 个组件`, `${group.components.length} ${group.components.length === 1 ? 'component' : 'components'}`)}</span></span><ChevronRight className="size-4 shrink-0 text-muted transition-transform group-open:rotate-90" /></summary>
        <div className="px-5 pb-5"><p className="text-xs font-semibold">{t('覆盖的组件', 'Covers')}</p><p className="mt-1 break-words text-xs leading-5 text-muted">{group.components.join(', ')}</p><div className="mt-4 border-t border-edge pt-4">{paragraphs(group.text).map((paragraph, index) => <p key={index} className="mt-3 break-words text-xs leading-5 text-muted first:mt-0">{paragraph}</p>)}</div></div>
      </details>)}</div>
    </section>
    <p className="pt-7 pb-3 text-center text-xs leading-6 text-muted">{t('此列表随 App 构建时生成。', 'This list is generated when the app is built.')}</p>
  </CloudScreen>;
}
export function NativeInstanceError({ instance }: { instance: MobileInstance }) {
  const t = useCopy();
  return <CloudScreen><Brand /><div className="flex flex-1 flex-col items-center justify-center py-10 text-center"><span className="mb-7 grid size-20 place-items-center rounded-3xl border border-gold/40 bg-surface/80 shadow-elevation-0"><AlertTriangle className="size-9 text-gold-ink" /></span><h1 className="text-3xl font-semibold leading-tight tracking-tight">{t('暂时无法打开实例', 'Could not open this instance')}</h1><p className="mt-5 text-sm leading-7 text-muted">{t('检查网络，或确认运行 Avibe 的电脑已联网。', 'Check your connection and that the computer running Avibe is online.')}</p><div className={cn(raised, 'mt-7 w-full break-all text-left')}><p className="text-sm font-semibold">{instance.name}</p><p className="mt-2 text-xs leading-6 text-muted">{instance.host}</p></div><div className="mt-8 flex w-full flex-col gap-3"><Primary>{t('重试', 'Retry')}</Primary><Button variant="outline" size="lg" className="w-full rounded-full">{t('切换实例', 'Switch instance')}</Button></div></div></CloudScreen>;
}
export function NativeWindowTopBar() {
  const t = useCopy();
  return <CloudScreen header={<MobileAppHeader title="docs.avibe.bot" icon={Globe} actions={<><Button variant="ghost" size="compact">{t('完成', 'Done')}</Button><Button variant="ghost" size="compact">{t('在 Safari 中打开', 'Open in Safari')}</Button></>} />}><div className="pt-8"><Globe className="size-8 text-mint-ink" /></div><PageTitle title={t('开始使用', 'Get started')} subtitle="Avibe" /><p className="text-sm leading-7 text-muted">{t('在电脑上运行 Avibe，连接你的 AI 伙伴。', 'Run Avibe on your computer and connect to your AI partner.')}</p></CloudScreen>;
}

/**
 * Second step of sign-in. Passkey first when the account has one; the
 * authenticator code and a recovery code are the alternatives. An account
 * that must turn it on (platform administrators) sets it up on avibe.bot.
 */
export function SecondFactorPage({ fixture }: { fixture: MobileSecondFactorFixture }) {
  const t = useCopy();
  const { email, methods, step, code = '', busy, error, compact = false } = fixture;
  const hasPasskey = methods.includes('passkey');
  const hasTotp = methods.includes('totp');
  const setup = step === 'setup';
  const notice = error === 'passkey-failed' ? t('通行密钥验证没有完成。可以再试一次，或改用验证码。', 'The passkey check didn’t finish. Try again, or use a code instead.')
    : error === 'code-invalid' ? (step === 'recovery' ? t('恢复码不正确，或已经用过。', 'That recovery code is wrong or already used.') : t('验证码不正确。请输入 App 里最新的 6 位数字。', 'That code isn’t right. Enter the latest 6 digits from your app.'))
      : error === 'rate-limited' ? t('尝试次数过多，请 15 分钟后再试。', 'Too many attempts. Try again in 15 minutes.')
        : error === 'network' ? t('暂时连不上 avibe.bot。检查网络后重试。', 'Could not reach avibe.bot. Check your connection and retry.') : null;
  const feedback = <div id="mobile-login-feedback" className="mt-3 min-h-6 text-center text-xs leading-6">{notice && <p role="alert" className="text-destructive-ink">{notice}</p>}</div>;
  const verify = (ready: boolean) => <div className="mt-5" aria-busy={busy === 'verifying'}><Primary disabled={Boolean(busy) || !ready}>{busy === 'verifying' ? t('验证中…', 'Verifying…') : t('验证', 'Verify')}</Primary></div>;
  const alternatives = step === 'passkey' ? [hasTotp && t('改用身份验证器验证码', 'Use an authenticator code instead'), t('使用恢复码', 'Use a recovery code')]
    : step === 'totp' ? [hasPasskey && t('改用通行密钥', 'Use your passkey instead'), t('使用恢复码', 'Use a recovery code')]
      : step === 'recovery' ? [hasPasskey ? t('改用通行密钥', 'Use your passkey instead') : t('改用身份验证器验证码', 'Use an authenticator code instead')] : [];
  return <CloudScreen><Brand /><div className={cn('flex flex-col items-center', compact ? 'pt-4 pb-4' : 'pt-8 pb-6')}><span className={cn('grid place-items-center rounded-3xl border border-edge bg-surface/80 shadow-elevation-0', compact ? 'mb-3 size-14' : 'mb-5 size-16')}><ShieldCheck className="size-7 text-mint-ink" /></span><h1 className={cn('text-center font-semibold leading-tight tracking-tight', compact ? 'text-2xl' : 'text-3xl')}>{setup ? t('先开启两步验证。', 'Turn on two-step verification first.') : t('确认是你本人。', 'Confirm it’s you.')}</h1></div>
    <div className={cn('text-center', compact ? 'pb-5' : 'pb-7')}><p className="text-sm leading-7 text-muted">{setup ? t('这个账号要求开启两步验证', 'This account requires two-step verification') : t('这个账号开启了两步验证', 'Two-step verification is on for')}</p><p className="break-all text-sm font-medium leading-6">{email}</p></div>
    {step === 'passkey' && <><div aria-busy={busy === 'passkey'}><Primary disabled={Boolean(busy)}><KeyRound className="size-4" />{busy === 'passkey' ? t('正在等待通行密钥…', 'Waiting for your passkey…') : t('使用通行密钥', 'Use your passkey')}</Primary></div><p id="mobile-login-hint" className="mt-5 text-center text-sm leading-6 text-muted">{t('用面容 ID、触控 ID 或设备密码确认。', 'Confirm with Face ID, Touch ID, or your device passcode.')}</p>{feedback}</>}
    {step === 'totp' && <><CodeInput code={code} invalid={error === 'code-invalid'} busy={Boolean(busy)} label={t('输入身份验证器里的 6 位验证码', 'Enter the 6-digit code from your authenticator app')} /><p id="mobile-login-hint" className="mt-5 text-center text-sm leading-6 text-muted">{t('打开身份验证器 App，输入 avibe.bot 的 6 位验证码', 'Open your authenticator app and enter the 6-digit code for avibe.bot')}</p>{feedback}{verify(code.length === 6)}</>}
    {step === 'recovery' && <><div className={cn(raised, 'flex flex-col gap-3')}><Label htmlFor="mobile-recovery-code">{t('恢复码', 'Recovery code')}</Label><Input id="mobile-recovery-code" type="text" autoComplete="one-time-code" autoCapitalize="none" spellCheck={false} defaultValue={code} placeholder="xxxxx-xxxxx" disabled={Boolean(busy)} aria-invalid={error === 'code-invalid' || undefined} aria-describedby="mobile-login-hint mobile-login-feedback" className="h-14 rounded-2xl px-4 text-base tracking-wide" /><p id="mobile-login-hint" className="text-xs leading-6 text-muted">{t('每个恢复码只能用一次。用掉之后，记得在 avibe.bot 重新生成一组。', 'Each recovery code works once. After using one, make a new set on avibe.bot.')}</p></div>{feedback}{verify(code.length > 0)}</>}
    {setup && <><div className={cn(raised, 'text-sm leading-7 text-muted')}>{t('在 avibe.bot 网页的「账号安全」里添加通行密钥或身份验证器 App，完成后回到这里继续。', 'On avibe.bot, open Account security and add a passkey or an authenticator app. Then come back here to continue.')}</div><div className="mt-5 flex flex-col gap-3"><Button variant="brand" size="lg" className="w-full gap-2 rounded-full"><ExternalLink className="size-4" />{t('打开 avibe.bot', 'Open avibe.bot')}</Button><Button variant="outline" size="lg" className="w-full gap-2 whitespace-normal rounded-full"><RotateCw className="size-4 shrink-0" />{t('我已开启，继续', 'I’ve turned it on · Continue')}</Button></div></>}
    {alternatives.some(Boolean) && <div className="mt-6 flex flex-col items-center gap-1">{alternatives.filter(Boolean).map(label => <Button key={label as string} variant="link" size="compact" disabled={Boolean(busy)} className="h-auto max-w-full whitespace-normal px-0 text-sm text-foreground underline">{label}</Button>)}</div>}
    <div className="mt-auto pt-7 pb-3 text-center"><Button variant="link" size="compact" disabled={Boolean(busy)} className="h-auto px-0 text-xs text-muted underline">{t('使用其他账号登录', 'Sign in with a different account')}</Button></div>
  </CloudScreen>;
}

/** The key tile that heads the forgot and weak-password pages, the same raised tile as two-step verification. */
function PageMark({ warn = false }: { warn?: boolean }) {
  return <span className={cn('mb-5 grid size-16 place-items-center rounded-3xl border bg-surface/80 shadow-elevation-0', warn ? 'border-gold/40' : 'border-edge')}>{warn ? <AlertTriangle className="size-7 text-gold-ink" /> : <KeyRound className="size-7 text-mint-ink" />}</span>;
}

/**
 * The email step in password mode: one form with the email (the saved login's
 * username) and the password. Switching never asks for the email first; Sign in
 * checks it. Errors that the email code resolves add "Use an email code instead".
 */
export function PasswordPage({ fixture }: { fixture: MobilePasswordFixture }) {
  const t = useCopy();
  const { email, password = '', shown = false, focused, busy = false, error, wait, compact = false } = fixture;
  const emailInvalid = error === 'email-invalid';
  const wrong = error === 'invalid-credentials';
  const codeFixes = wrong || error === 'email-not-confirmed';
  const notice = wrong ? t('邮箱或密码不正确。还没设置过密码？请改用邮箱验证码登录。', 'Wrong email or password. Haven’t set a password? Use an email code instead.')
    : error === 'email-not-confirmed' ? t('这个邮箱还没有确认。用邮箱验证码登录，即可完成确认。', 'This email isn’t confirmed yet. Sign in with an email code to confirm it.')
      : error === 'rate-limited' ? t('尝试次数过多，请稍后再试。', 'Too many attempts. Please try again shortly.')
        : error === 'network' ? t('暂时连不上 avibe.bot。检查网络后重试。', 'Could not reach avibe.bot. Check your connection and retry.') : null;
  const ring = 'border-ring ring-2 ring-ring';
  return <CloudScreen><Brand back />{compact ? <div className="pt-3" /> : <div className="flex flex-col items-center pt-8 pb-6"><img src={mailMascot} alt="" className="mb-5 size-28 object-contain" /><h1 className="text-center text-3xl font-semibold leading-tight tracking-tight">{t('从一封邮件开始。', 'It starts with an email.')}</h1><p className="mt-3 text-center text-sm leading-6 text-muted">{t('登录 / 注册你的 avibe.bot 账号', 'Sign in or create your avibe.bot account')}</p></div>}
    <form className={cn('rounded-3xl border border-edge bg-surface/70 shadow-elevation-0', compact ? 'p-5' : 'p-6')}><div className={cn('flex flex-col', compact ? 'gap-4' : 'gap-6')}>
      <div className="flex flex-col gap-3"><Label htmlFor="mobile-login-email">{t('邮箱', 'Email')}</Label><Input id="mobile-login-email" name="username" type="email" inputMode="email" autoComplete="username" autoCapitalize="none" spellCheck={false} enterKeyHint="next" defaultValue={email} placeholder="you@example.com" disabled={busy} aria-invalid={emailInvalid || undefined} aria-describedby="mobile-login-email-feedback" className={cn('h-14 rounded-2xl px-4 text-base', emailInvalid && 'border-destructive', focused === 'email' && !emailInvalid && ring)} />{emailInvalid && <div id="mobile-login-email-feedback"><p role="alert" className="text-xs leading-6 text-destructive-ink">{t('请填写有效的邮箱。', 'Enter a valid email address.')}</p></div>}</div>
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3"><Label htmlFor="mobile-login-password">{t('密码', 'Password')}</Label><Button variant="link" size="compact" disabled={busy} className="h-auto px-0 text-sm text-muted underline">{t('忘记密码？', 'Forgot password?')}</Button></div>
        <div className="relative"><Input id="mobile-login-password" name="password" type={shown ? 'text' : 'password'} autoComplete="current-password" autoCapitalize="none" spellCheck={false} enterKeyHint="go" defaultValue={password} disabled={busy} aria-invalid={wrong || undefined} aria-describedby="mobile-login-password-feedback" className={cn('h-14 rounded-2xl pr-14 pl-4 text-base', wrong && 'border-destructive', focused === 'password' && !wrong && ring)} /><Button type="button" variant="ghost" size="icon" disabled={busy} aria-pressed={shown} aria-label={t('显示密码', 'Show password')} className="absolute top-1/2 right-1.5 size-11 -translate-y-1/2 rounded-xl text-muted">{shown ? <EyeOff className="size-5" /> : <Eye className="size-5" />}</Button></div>
        <div id="mobile-login-password-feedback" className={cn(!compact && 'min-h-6')}>{notice && <p role="alert" className="text-xs leading-6 text-destructive-ink">{notice}</p>}</div>
      </div>
      <div className="flex flex-col gap-3"><div aria-busy={busy}><Primary disabled={busy || !password || Boolean(wait)}>{busy ? t('登录中…', 'Signing in…') : wait ? t(`${wait} 秒后可重试`, `Try again in ${wait}s`) : t('登录', 'Sign in')}</Primary></div><Button variant="link" size="compact" disabled={busy} className={cn(alternative, 'gap-2 self-center')}><Mail className="size-4" />{t('使用验证码登录', 'Sign in with an email code')}</Button></div>
    </div></form>
    {codeFixes && <div className="mt-6"><Button variant="outline" size="lg" disabled={busy} className="w-full gap-2 whitespace-normal rounded-full"><Mail className="size-4 shrink-0" />{t('改用邮箱验证码登录', 'Use an email code instead')}</Button></div>}
    {!compact && <p className="mt-auto pt-7 pb-3 text-center text-xs leading-6 text-muted">{t('还没有密码？用邮箱验证码登录后，可以在 avibe.bot 的「账号安全」里设置。', 'No password yet? Sign in with an email code, then set one in Account security on avibe.bot.')}</p>}
  </CloudScreen>;
}

/**
 * Forgot password. Email sign-in always works, so the code comes first; a new
 * password is set on avibe.bot's Account security page, opened in Safari.
 */
export function ForgotPasswordPage({ email }: { email: string }) {
  const t = useCopy();
  return <CloudScreen><Brand back /><div className="flex flex-col items-center pt-8 pb-6"><PageMark /><h1 className="text-center text-3xl font-semibold leading-tight tracking-tight">{t('忘记密码了？', 'Forgot your password?')}</h1><p className="mt-3 text-center text-sm leading-6 text-muted">{t('邮件登录始终可用。', 'Email sign-in always works.')}</p></div>
    <div className="pb-7 text-center"><p className="text-sm leading-7 text-muted">{t('账号', 'Account')}</p><p className="break-all text-sm font-medium leading-6">{email}</p></div>
    <section className={cn(raised, 'flex flex-col gap-4')}><div><h2 className="text-base font-semibold">{t('现在登录', 'Sign in now')}</h2><p className="mt-2 text-sm leading-6 text-muted">{t('验证码会发到这个邮箱，在这里输入就能登录。', 'We’ll email a code to this address. Enter it here to sign in.')}</p></div><Primary>{t('发送验证码', 'Send verification code')}</Primary></section>
    <section className={cn(raised, 'mt-4 flex flex-col gap-4')}><div><h2 className="text-base font-semibold">{t('设置新密码', 'Set a new password')}</h2><p className="mt-2 text-sm leading-6 text-muted">{t('在 Safari 中打开 avibe.bot 的「账号安全」，用邮件登录后，选择「设置或修改密码」。', 'Open Account security on avibe.bot in Safari. Sign in with your email, then choose Set or change password.')}</p></div><Button variant="outline" size="lg" className="w-full gap-2 whitespace-normal rounded-full"><ExternalLink className="size-4 shrink-0" />{t('在 Safari 中打开', 'Open in Safari')}</Button></section>
  </CloudScreen>;
}

/**
 * Signed in, but avibe.bot reports the password as weak (it appeared in a data
 * breach). A one-time notice before the instance picker; Continue never blocks.
 */
export function WeakPasswordNotice({ email }: { email: string }) {
  const t = useCopy();
  return <CloudScreen><Brand /><div className="flex flex-1 flex-col items-center justify-center py-10 text-center"><PageMark warn /><h1 className="text-3xl font-semibold leading-tight tracking-tight">{t('建议更换密码。', 'Time for a new password.')}</h1><p className="mt-4 text-sm leading-7 text-muted">{t('已登录', 'Signed in as')}</p><p className="break-all text-sm font-medium leading-6">{email}</p>
    <div className={cn(raised, 'mt-7 w-full text-left text-sm leading-7 text-muted')}>{t('这个密码出现在已知的数据泄露中，别人可能猜到它。请在 avibe.bot 的「账号安全」里换一个新密码。', 'This password has appeared in a data breach, so others may guess it. Choose a new one in Account security on avibe.bot.')}</div>
    <div className="mt-8 flex w-full flex-col gap-3"><Primary>{t('继续', 'Continue')}</Primary><Button variant="outline" size="lg" className="w-full gap-2 whitespace-normal rounded-full"><ExternalLink className="size-4 shrink-0" />{t('在 Safari 中更换密码', 'Change it in Safari')}</Button></div></div>
  </CloudScreen>;
}
