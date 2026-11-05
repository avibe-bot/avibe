import type { ReactNode } from 'react';
import {
  ArrowLeft, ArrowRight, ChevronRight, Laptop, Mail, Plus, RotateCw,
  ShieldCheck, Sparkles, UserRound,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import logo from '@/assets/logo.png';
import mascot from '@/assets/pet/vibey.png';
import mailMascot from '@/assets/pet/vibey-message.png';
import heroMascot from '../../../assets/mascot/cloud-tuanzi-v2.png';
import { cn } from '@/lib/utils';
import { PhoneScreen } from '../shell/PhoneScreen';
import type { MobileInstance } from './mobile-app';

export type MobileDirection = 'a' | 'b' | 'c';
export type DirectionSurface = 'welcome' | 'email' | 'code' | 'instances';
export interface DirectionFixture {
  email: string;
  code: string;
  instances: MobileInstance[];
}

/** Direction studies only. The selected direction will receive the full state matrix. */
function DirectionShell({ direction, surface, children }: { direction: MobileDirection; surface: DirectionSurface; children: ReactNode }) {
  return <div className="h-full [&>div>main]:pb-0">
    <PhoneScreen header={<></>} tabBar={<></>} bare flat>
      <div className={cn('relative flex min-h-0 flex-1 flex-col overflow-y-auto', direction === 'c' && 'bg-surface')}
        style={direction === 'a' ? { background: 'var(--gradient-wizard-platforms)' } : undefined}>
        <div className={cn('relative flex min-h-full flex-col p-6', direction === 'c' && surface === 'code' && 'bg-mint-soft')}>{children}</div>
      </div>
    </PhoneScreen>
  </div>;
}

function Brand({ direction, back = false, account = false }: { direction: MobileDirection; back?: boolean; account?: boolean }) {
  return <header className="relative z-10 flex h-10 shrink-0 items-center justify-between">
    <div className="flex items-center gap-2.5">
      {back ? <Button variant="ghost" size="icon" aria-label="返回"><ArrowLeft className="size-5" /></Button>
        : <img src={logo} alt="" className="size-8" />}
      <span className={cn('text-lg font-semibold tracking-tight', direction === 'b' && 'font-bold')}>Avibe</span>
    </div>
    {account ? <Button variant="ghost" size="icon" className="rounded-full border border-border" aria-label="账户"><UserRound className="size-4" /></Button>
      : <span className="text-xs text-muted">Agent OS</span>}
  </header>;
}

function Primary({ direction, children, disabled = false }: { direction: MobileDirection; children: ReactNode; disabled?: boolean }) {
  return <Button size="lg" disabled={disabled}
    className={cn('w-full gap-3', direction === 'a' ? 'rounded-full shadow-mint-card-sm' : direction === 'b' ? 'rounded-2xl shadow-glow-sm-mint' : 'rounded-xl')}
    variant={direction === 'c' ? 'default' : 'brand'}>
    {children}<ArrowRight className="size-4" />
  </Button>;
}

function PartnerNote({ centered = false }: { centered?: boolean }) {
  return <p className={cn('text-xs leading-relaxed text-muted', centered && 'text-center')}>
    Vibey，你的内置 AI 伙伴。<br />连接自己的实例，也可以加入别人共享的实例。
  </p>;
}

function Welcome({ direction }: { direction: MobileDirection }) {
  if (direction === 'a') return <>
    <Brand direction={direction} />
    <div className="relative flex flex-1 flex-col items-center justify-center py-8">
      <div className="relative mb-8 grid size-64 place-items-center">
        <div className="absolute inset-0 rounded-full border border-mint/20 bg-surface/30 shadow-mint-card-lg" />
        <div className="absolute inset-6 rounded-full border border-surface/70 bg-surface/30" />
        <img src={heroMascot} alt="Vibey 团子" className="relative size-52 object-contain" />
        <span className="absolute -right-1 top-9 grid size-12 rotate-12 place-items-center rounded-2xl border border-border bg-surface shadow-mint-card-sm"><Sparkles className="size-5 text-mint-ink" /></span>
        <span className="absolute -left-1 bottom-5 rounded-full border border-border bg-surface px-4 py-2 text-sm shadow-mint-card-sm">你好，我是 Vibey</span>
      </div>
      <h1 className="text-center text-4xl font-semibold leading-tight tracking-tight">你的 AI 伙伴，<br />随身同行。</h1>
      <p className="mt-5 max-w-xs text-center text-sm leading-7 text-muted">电脑上的 Avibe，<br />现在也在你的口袋里。</p>
    </div>
    <div className="flex flex-col gap-4 pb-3"><Primary direction={direction}>登录 / 注册</Primary><PartnerNote centered /></div>
  </>;
  if (direction === 'b') return <>
    <Brand direction={direction} />
    <div className="flex flex-1 flex-col justify-center py-10">
      <h1 className="text-5xl font-bold leading-tight tracking-tight">把工作台<br />带在身边<span className="text-mint-ink">。</span></h1>
      <div className="relative my-10 flex h-52 flex-col justify-between overflow-hidden rounded-3xl border border-mint/30 bg-mint-soft p-6 shadow-mint-card">
        <div className="flex items-start justify-between"><Laptop className="size-7 text-mint-ink" /><span className="text-sm font-medium text-mint-ink">Avibe · 随时连接</span></div>
        <div className="relative flex items-end justify-between"><p className="text-2xl font-semibold leading-snug">想法不停，<br />随时继续。</p><img src={mascot} alt="Vibey 团子" className="size-24 object-contain" /></div>
      </div>
      <p className="text-sm leading-7 text-muted">从手机连接你的 Agent OS，<br />和 AI 伙伴一起推进下一步。</p>
    </div>
    <div className="flex flex-col gap-4 pb-3"><Primary direction={direction}>登录 / 注册</Primary><p className="text-center text-xs text-muted">自己的实例，与他人共享的工作空间。</p></div>
  </>;
  return <>
    <Brand direction={direction} />
    <div className="flex flex-1 flex-col justify-center py-10">
      <div className="mb-10 flex items-end gap-3"><img src={mascot} alt="Vibey 团子" className="size-24 object-contain" /><span className="mb-4 text-sm text-muted">你好，Vibey。</span></div>
      <h1 className="text-4xl font-medium leading-snug tracking-tight">让每个想法，<br />都有下文。</h1>
      <p className="mt-6 text-sm leading-7 text-muted">连接你的 Avibe。<br />在手机上，继续和 AI 伙伴一起工作。</p>
      <div className="mt-10 h-px w-12 bg-mint" />
    </div>
    <div className="flex flex-col gap-5 pb-4"><Primary direction={direction}>登录 / 注册</Primary><PartnerNote /></div>
  </>;
}

function EmailForm({ direction, email }: { direction: MobileDirection; email: string }) {
  return <div className="flex flex-col gap-6">
    <div className="flex flex-col gap-3">
      <Label htmlFor={`direction-${direction}-email`}>邮箱</Label>
      <Input id={`direction-${direction}-email`} type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false} defaultValue={email}
        className={cn('h-14 px-4 text-base', direction === 'a' ? 'rounded-2xl bg-surface shadow-sm' : direction === 'b' ? 'rounded-xl border-border-strong bg-surface' : 'rounded-xl bg-background shadow-none')} />
      <p className="text-xs leading-6 text-muted">新邮箱将自动注册，无需设置密码。</p>
    </div>
    <Primary direction={direction}>发送验证码</Primary>
  </div>;
}

function Email({ direction, email }: { direction: MobileDirection; email: string }) {
  return <>
    <Brand direction={direction} back />
    {direction === 'a' ? <>
      <div className="flex flex-col items-center pt-9 pb-8"><img src={mailMascot} alt="" className="mb-5 size-32 object-contain" /><h1 className="text-3xl font-semibold tracking-tight">从一封邮件开始。</h1><p className="mt-3 text-sm text-muted">登录 / 注册你的 avibe.bot 账号</p></div>
      <div className="rounded-3xl border border-surface/70 bg-surface/70 p-6 shadow-mint-card"><EmailForm direction={direction} email={email} /></div>
      <p className="mt-auto pt-8 pb-4 text-center text-xs text-muted">验证码会送到你的邮箱，在这里完成登录。</p>
    </> : direction === 'b' ? <>
      <div className="pt-16 pb-10"><h1 className="text-5xl font-bold leading-tight tracking-tight">准备好，<br /><span className="text-mint-ink">开始连接。</span></h1><p className="mt-6 text-sm text-muted">登录 / 注册 · 使用你的邮箱</p></div>
      <EmailForm direction={direction} email={email} />
      <div className="mt-auto flex items-center gap-4 border-t border-border pt-6 pb-4"><span className="grid size-12 shrink-0 place-items-center rounded-2xl bg-mint-soft"><Mail className="size-5 text-mint-ink" /></span><p className="text-xs leading-6 text-muted">我们会发送一封验证码邮件。<br />不需要离开 App 完成登录。</p></div>
    </> : <>
      <div className="pt-20 pb-12"><span className="mb-7 grid size-12 place-items-center rounded-2xl bg-mint-soft"><Mail className="size-5 text-mint-ink" /></span><h1 className="text-3xl font-medium tracking-tight">你的邮箱。</h1><p className="mt-4 text-sm leading-7 text-muted">登录 / 注册 avibe.bot，<br />下一步，输入邮件里的验证码。</p></div>
      <EmailForm direction={direction} email={email} />
      <p className="mt-auto pt-12 pb-4 text-xs leading-6 text-muted">一个账号，连接自己的与他人共享的实例。</p>
    </>}
  </>;
}

/** One accessible input; the six boxes are a fixture-driven decorative display. */
export function DirectionCodeInput({ direction, code }: { direction: MobileDirection; code: string }) {
  return <div className="relative">
    <Label htmlFor={`direction-${direction}-code`} className="sr-only">输入邮件里的 6 位验证码</Label>
    <Input id={`direction-${direction}-code`} className="sr-only" type="text" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} defaultValue={code} aria-describedby={`direction-${direction}-code-help`} />
    <div aria-hidden="true" className="grid grid-cols-6 gap-2">
      {Array.from({ length: 6 }, (_, index) => <span key={index}
        className={cn('grid aspect-square min-w-0 place-items-center rounded-2xl border text-2xl font-medium',
          direction === 'a' ? 'border-surface/80 bg-surface shadow-mint-card-sm' : direction === 'b' ? 'border-border-strong bg-surface font-mono' : 'border-transparent bg-surface shadow-sm',
          index === code.length && 'border-mint ring-2 ring-mint/30',
          index === code.length && direction === 'b' && 'shadow-glow-sm-mint')}>
        {code[index] || (index === code.length ? <span className="h-6 w-px bg-mint-ink" /> : null)}
      </span>)}
    </div>
  </div>;
}

function Code({ direction, email, code }: { direction: MobileDirection; email: string; code: string }) {
  return <>
    <Brand direction={direction} back />
    {direction === 'a' ? <div className="flex justify-center pt-10 pb-6"><img src={mailMascot} alt="" className="size-32 object-contain" /></div>
      : direction === 'b' ? <div className="pt-14"><ShieldCheck className="size-10 text-mint-ink" /></div>
      : <div className="pt-16"><span className="grid size-14 place-items-center rounded-2xl bg-mint-soft"><Mail className="size-6 text-mint-ink" /></span></div>}
    <div className={cn('pb-8', direction === 'a' ? 'text-center' : 'pt-7')}>
      <h1 className={cn('tracking-tight', direction === 'b' ? 'text-5xl font-bold leading-tight' : 'text-3xl font-semibold')}>{direction === 'b' ? <>验证邮箱，<br />即刻连接。</> : '查看你的邮箱。'}</h1>
      <p className="mt-5 text-sm leading-7 text-muted">验证码已发送至</p>
      <p className="break-all text-sm font-medium leading-6">{email}</p>
      <Button variant="link" size="compact" className="mt-1 px-0 text-muted underline">修改邮箱</Button>
    </div>
    <DirectionCodeInput direction={direction} code={code} />
    <p id={`direction-${direction}-code-help`} className={cn('mt-5 text-sm text-muted', direction === 'a' && 'text-center')}>输入邮件里的 6 位验证码</p>
    <div className="mt-8"><Primary direction={direction} disabled>验证并登录</Primary></div>
    <p className="mt-6 text-center text-sm text-muted">没收到验证码？<Button variant="link" size="compact" className="ml-1 h-auto px-0 text-sm text-foreground underline">重新发送</Button></p>
    <p className="mt-auto pt-9 pb-3 text-center text-xs leading-6 text-muted">可直接粘贴验证码。<br />邮件链接会打开 Safari，请在 App 内输入验证码。</p>
  </>;
}

function DirectionInstanceRow({ direction, instance }: { direction: MobileDirection; instance: MobileInstance }) {
  return <button type="button" className={cn('w-full text-left',
    direction === 'a' ? 'rounded-3xl border border-surface/80 bg-surface/80 p-5 shadow-mint-card-sm'
      : direction === 'b' ? 'rounded-2xl border border-border bg-surface p-5'
        : 'border-b border-border py-6')}>
    <div className="flex items-start gap-3">
      <span className={cn('grid size-11 shrink-0 place-items-center', direction === 'c' ? 'rounded-xl bg-background' : 'rounded-2xl bg-mint-soft')}><Laptop className={cn('size-5', instance.online ? 'text-mint-ink' : 'text-muted')} /></span>
      <span className="min-w-0 flex-1">
        <span className="block break-words text-base font-semibold">{instance.name}</span>
        <span className="mt-1 block break-all text-xs leading-5 text-muted">{instance.host}</span>
        <span className="mt-3 flex flex-wrap items-center gap-2"><Badge variant={instance.online ? 'success' : 'secondary'}>{instance.online ? '在线' : '离线'}</Badge>{instance.owner && <Badge variant="info">共享</Badge>}<span className="text-xs text-muted">{instance.owner ? `${instance.owner} 共享` : '我的实例'}</span></span>
      </span>
      <ChevronRight className="mt-3 size-4 shrink-0 text-muted" />
    </div>
  </button>;
}

function Instances({ direction, instances }: { direction: MobileDirection; instances: MobileInstance[] }) {
  return <>
    <Brand direction={direction} account />
    <div className={cn('flex items-end justify-between gap-3 pb-7', direction === 'a' ? 'pt-10' : 'pt-12')}>
      <div><h1 className={cn('leading-tight tracking-tight', direction === 'b' ? 'text-4xl font-bold' : 'text-3xl font-semibold')}>{direction === 'a' ? '想从哪里开始？' : direction === 'b' ? <>选个实例。<br /><span className="text-mint-ink">继续创造。</span></> : '选择你的实例。'}</h1><p className="mt-4 text-xs leading-6 text-muted">自己的实例，以及别人共享的实例</p></div>
      <Button variant="ghost" size="icon" className="shrink-0 rounded-full" aria-label="刷新实例"><RotateCw className="size-4" /></Button>
    </div>
    <div className={cn('flex flex-col', direction === 'a' ? 'gap-4' : direction === 'b' ? 'gap-3' : 'border-t border-border')}>
      {instances.map(instance => <DirectionInstanceRow key={instance.id} direction={direction} instance={instance} />)}
    </div>
    <div className="mt-auto pt-8 pb-3"><Button variant={direction === 'a' ? 'outline' : 'text'} size="lg" className={cn('w-full gap-2', direction === 'a' && 'rounded-full')}><Plus className="size-4" />添加自己的实例</Button></div>
  </>;
}

export function MobileDirectionScreen({ direction, surface, fixture }: { direction: MobileDirection; surface: DirectionSurface; fixture: DirectionFixture }) {
  return <DirectionShell direction={direction} surface={surface}>
    {surface === 'welcome' ? <Welcome direction={direction} />
      : surface === 'email' ? <Email direction={direction} email={fixture.email} />
        : surface === 'code' ? <Code direction={direction} email={fixture.email} code={fixture.code} />
          : <Instances direction={direction} instances={fixture.instances} />}
  </DirectionShell>;
}
