// Proposal (Track B, 2026-11-13): the iOS App's account and instance pages as one sheet.
// The avatar on an instance's header opens a native sheet (UISheetPresentationController) that
// holds the bundled page over the live instance: where you are, where else you can go, and a drag
// down (or Close) back to where you were. The account is one row at its bottom, opening a page
// inside the sheet. First use (nothing selected yet) is the same list as a full page.
// Plan: docs/plans/2026-11-13-ios-instance-pages.md. Static: no handlers, no effects.
import { createContext, useContext, type ReactNode } from 'react';
import {
  AlertTriangle, ArrowDown, ArrowRight, Check, ChevronDown, ChevronLeft, ChevronRight, ExternalLink,
  Laptop, Loader2, Plus, RotateCw, Users, WifiOff, X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Workbench } from '@/components/Workbench';
import { accountHeaderAvatarClassName, accountInitialFor } from '@/components/AccountAvatar';
import { LicensesEntry } from '@/mobile/LicensesPage';
import logo from '@/assets/logo.png';
import { cn } from '@/lib/utils';
import { phoneShell } from '../frames/workbench/fixtures';

export type SheetLocale = 'zh' | 'en';
export interface SheetInstance {
  id: string;
  name: string;
  host: string;
  /** Shared by this owner; absent for the account's own. */
  owner?: string;
  online: boolean;
}

const Locale = createContext<SheetLocale>('zh');
export function SheetLanguage({ lang, children }: { lang: SheetLocale; children: ReactNode }) {
  return <Locale.Provider value={lang}><div lang={lang} className="h-full">{children}</div></Locale.Provider>;
}
function useCopy() {
  const lang = useContext(Locale);
  return (zh: string, en: string) => (lang === 'en' ? en : zh);
}

const home = phoneShell('/', <Workbench />);
const group = 'overflow-hidden rounded-2xl border border-edge bg-surface';
const sectionLabel = 'mb-2 px-1 text-xs font-medium text-muted';

/** The instance underneath, shrunk and dimmed the way iOS presents a large sheet. */
function Underneath({ children, named }: { children?: ReactNode; named?: string }) {
  return (
    <div className="relative h-full overflow-hidden bg-surface-3">
      <div className="h-full origin-top scale-95 overflow-hidden rounded-3xl opacity-80">
        <div className="relative h-full">
          {home()}
          {named && <NamedHeader name={named} />}
        </div>
      </div>
      {children}
    </div>
  );
}

/** The sheet card: grabber, a header, and a scrolling body above the Home indicator. */
function SheetCard({ header, children, className }: { header: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section
      aria-modal="true"
      className={cn('absolute inset-x-0 bottom-0 top-[calc(env(safe-area-inset-top)+0.75rem)] flex flex-col rounded-t-3xl border-t border-edge bg-background shadow-elevation-3-up', className)}
    >
      <span aria-hidden="true" className="mx-auto mt-2 h-1.5 w-10 shrink-0 rounded-full bg-muted/40" />
      {header}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-[calc(env(safe-area-inset-bottom)+1.5rem)]">{children}</div>
    </section>
  );
}

function SheetHeader({ current }: { current: string }) {
  const t = useCopy();
  return (
    <header className="flex shrink-0 items-center justify-between gap-3 px-5 pt-3 pb-4">
      <h2 className="text-xl font-semibold tracking-tight">{t('实例', 'Instances')}</h2>
      <Button variant="ghost" size="icon" className="rounded-full bg-field" aria-label={t(`关闭，回到 ${current}`, `Close and return to ${current}`)}>
        <X className="size-4" />
      </Button>
    </header>
  );
}

function InstanceIcon({ instance, tone = 'plain' }: { instance: SheetInstance; tone?: 'current' | 'plain' }) {
  const Icon = instance.owner ? Users : Laptop;
  return (
    <span className={cn('grid size-10 shrink-0 place-items-center rounded-xl', tone === 'current' ? 'bg-mint-soft' : 'bg-field')}>
      <Icon className={cn('size-5', !instance.online ? 'text-muted' : tone === 'current' ? 'text-mint-ink' : 'text-foreground')} />
    </span>
  );
}

/** Where you are. Tapping it closes the sheet: you are already here. */
function CurrentInstance({ instance }: { instance: SheetInstance }) {
  const t = useCopy();
  return (
    <section>
      <h3 className={sectionLabel}>{t('正在使用', 'You’re in')}</h3>
      <button type="button" className="flex w-full items-center gap-3 rounded-2xl border border-mint/50 bg-surface p-4 text-left">
        <InstanceIcon instance={instance} tone="current" />
        <span className="min-w-0 flex-1">
          <span className="block break-words text-base font-semibold leading-6">{instance.name}</span>
          <span className="mt-0.5 block break-all text-xs leading-5 text-muted">{subline(instance, t)}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1 text-xs font-medium text-mint-ink"><Check className="size-4" />{t('当前', 'Current')}</span>
      </button>
    </section>
  );
}

function subline(instance: SheetInstance, t: ReturnType<typeof useCopy>) {
  return instance.owner ? t(`${instance.owner} 共享 · ${instance.host}`, `Shared by ${instance.owner} · ${instance.host}`) : instance.host;
}

export type RowState = 'idle' | 'disabled' | 'opening' | 'failed' | 'offline-open';

function InstanceListRow({ instance, state = 'idle' }: { instance: SheetInstance; state?: RowState }) {
  const t = useCopy();
  const offline = !instance.online;
  // A failed row holds its own Retry, so the row itself is not a button then.
  const Row = state === 'failed' ? 'div' : 'button';
  return (
    <div className={cn(state === 'disabled' && 'opacity-50')}>
      <Row {...(Row === 'button' ? { type: 'button' as const } : {})} className="flex min-h-16 w-full items-center gap-3 px-4 py-3 text-left" aria-expanded={offline ? state === 'offline-open' : undefined}>
        <InstanceIcon instance={instance} />
        <span className="min-w-0 flex-1">
          <span className={cn('block break-words text-sm font-semibold leading-5', offline && 'text-muted')}>{instance.name}</span>
          {state === 'opening' ? (
            <span role="status" className="mt-0.5 block text-xs leading-5 text-mint-ink">{t('正在打开…', 'Opening…')}</span>
          ) : state === 'failed' ? (
            <span role="alert" className="mt-0.5 block text-xs leading-5 text-destructive-ink">{t(`没能打开：${instance.name} 没有响应`, `Couldn’t open: ${instance.name} didn’t respond`)}</span>
          ) : (
            <span className="mt-0.5 block break-all text-xs leading-5 text-muted">{offline ? t(`离线 · ${instance.host}`, `Offline · ${instance.host}`) : subline(instance, t)}</span>
          )}
        </span>
        {state === 'opening' ? (
          <Loader2 className="size-4 shrink-0 animate-spin text-mint-ink" aria-hidden="true" />
        ) : state === 'failed' ? (
          <Button variant="outline" size="sm" className="shrink-0 rounded-full">{t('重试', 'Retry')}</Button>
        ) : offline ? (
          <ChevronDown className={cn('size-4 shrink-0 text-muted', state === 'offline-open' && 'rotate-180')} aria-hidden="true" />
        ) : (
          <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden="true" />
        )}
      </Row>
      {state === 'offline-open' && (
        <div role="status" className="mx-4 mb-4 rounded-xl bg-field p-3 text-xs leading-5 text-muted">
          <p>{t(`${instance.name} 现在离线。在那台电脑上打开 Avibe 并联网，它就会出现为在线。`, `${instance.name} is offline. Open Avibe on that computer and connect it to the internet, and it shows as online.`)}</p>
          <Button variant="link" size="sm" className="mt-1 h-auto px-0 text-xs text-foreground"><RotateCw className="size-3" />{t('刷新列表', 'Refresh the list')}</Button>
        </div>
      )}
    </div>
  );
}

function OtherInstances({ instances, states = {}, status }: { instances: SheetInstance[]; states?: Record<string, RowState>; status?: 'sorted' | 'refreshing' }) {
  const t = useCopy();
  return (
    <section className="mt-6">
      <div className="flex items-baseline justify-between">
        <h3 className={sectionLabel}>{t('其他实例', 'Other instances')}</h3>
        {status === 'refreshing' ? (
          <span role="status" className="mb-2 flex items-center gap-1.5 px-1 text-xs text-muted"><Loader2 className="size-3 animate-spin" />{t('正在更新…', 'Updating…')}</span>
        ) : (
          <span className="mb-2 px-1 text-xs text-muted">{t('最近使用的在前', 'Recently used first')}</span>
        )}
      </div>
      {instances.length === 0 ? (
        <div className={cn(group, 'px-4 py-5')}>
          <p className="text-sm font-semibold">{t('还没有其他实例', 'No other instances yet')}</p>
          <p className="mt-1 text-xs leading-5 text-muted">{t('在另一台电脑上安装 Avibe 并用这个账号配对，它会出现在这里。', 'Install Avibe on another computer and pair it with this account, and it appears here.')}</p>
        </div>
      ) : (
        <div className={cn(group, 'divide-y divide-border')}>
          {instances.map((instance) => <InstanceListRow key={instance.id} instance={instance} state={states[instance.id]} />)}
        </div>
      )}
    </section>
  );
}

function AddInstanceRow() {
  const t = useCopy();
  return (
    <div className={cn(group, 'mt-3')}>
      <button type="button" className="flex min-h-14 w-full items-center gap-3 px-4 py-3 text-left">
        <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-field"><Plus className="size-5 text-mint-ink" /></span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold">{t('添加实例', 'Add an instance')}</span>
          <span className="mt-0.5 block text-xs leading-5 text-muted">{t('在电脑或服务器上安装 Avibe', 'Install Avibe on a computer or server')}</span>
        </span>
        <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden="true" />
      </button>
    </div>
  );
}

function AccountRow({ email }: { email: string }) {
  const t = useCopy();
  return (
    <section className="mt-8">
      <h3 className={sectionLabel}>{t('账户', 'Account')}</h3>
      <div className={group}>
        <button type="button" className="flex min-h-14 w-full items-center gap-3 px-4 py-3 text-left">
          <span className={cn(accountHeaderAvatarClassName, 'size-10 text-sm')} aria-hidden="true">{accountInitialFor(email)}</span>
          <span className="min-w-0 flex-1">
            <span className="block break-all text-sm font-semibold leading-5">{email}</span>
            <span className="mt-0.5 block text-xs leading-5 text-muted">{t('退出登录、开源许可', 'Sign out, open-source licenses')}</span>
          </span>
          <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden="true" />
        </button>
      </div>
    </section>
  );
}

function StaleBanner() {
  const t = useCopy();
  return (
    <div role="status" className="mb-4 flex items-start gap-2.5 rounded-xl border border-gold/40 bg-gold-soft px-3 py-2.5 text-xs leading-5">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold-ink" />
      <p className="min-w-0 flex-1">{t('没能更新实例列表，下面是上次的结果，在线状态可能已经变了。', 'Couldn’t update the list. These are the last results; who is online may have changed.')}</p>
      <Button variant="link" size="sm" className="h-auto shrink-0 px-0 text-xs font-semibold text-foreground">{t('重试', 'Retry')}</Button>
    </div>
  );
}

/** A labelled gesture, drawn on the frame: what the finger does and where it leads. */
function GestureNote({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div aria-hidden="true" className={cn('absolute z-10 flex items-center gap-2 rounded-full bg-foreground px-3 py-1.5 text-xs font-medium text-background shadow-elevation-2', className)}>
      {children}
    </div>
  );
}

export type SheetState = 'default' | 'offline-open' | 'opening' | 'open-failed' | 'stale' | 'refreshing' | 'only-current' | 'dismissing';

/** Variant A: the avatar opens this sheet. Variant B (`named`): the instance name in the header opens it, and the avatar opens Account directly, so the sheet has no account row. */
export function InstanceSheet({ state, current, others, email, named = false }: { state: SheetState; current: SheetInstance; others: SheetInstance[]; email: string; named?: boolean }) {
  const t = useCopy();
  const opening = others.find((each) => each.online && each.id !== current.id);
  const offline = others.find((each) => !each.online);
  const states: Record<string, RowState> = {};
  if (state === 'opening' && opening) for (const each of others) states[each.id] = each.id === opening.id ? 'opening' : 'disabled';
  if (state === 'open-failed' && opening) states[opening.id] = 'failed';
  if (state === 'offline-open' && offline) states[offline.id] = 'offline-open';
  return (
    <Underneath named={named ? current.name : undefined}>
      <SheetCard header={<SheetHeader current={current.name} />} className={state === 'dismissing' ? 'translate-y-1/4' : undefined}>
        {state === 'stale' && <StaleBanner />}
        <CurrentInstance instance={current} />
        <OtherInstances instances={state === 'only-current' ? [] : others} states={states} status={state === 'refreshing' ? 'refreshing' : 'sorted'} />
        <AddInstanceRow />
        {!named && <AccountRow email={email} />}
      </SheetCard>
      {state === 'dismissing' && (
        <GestureNote className="top-[calc(env(safe-area-inset-top)+6rem)] left-1/2 -translate-x-1/2">
          <ArrowDown className="size-4" />{t(`下拉关闭，回到 ${current.name}`, `Drag down to return to ${current.name}`)}
        </GestureNote>
      )}
    </Underneath>
  );
}

/** Account, pushed inside the sheet. Back (or a swipe from the left edge) returns to the list. */
function AccountPageBody({ email }: { email: string }) {
  const t = useCopy();
  return (
    <>
      <section className={cn(group, 'p-5')}>
        <p className="text-xs text-muted">{t('当前登录', 'Signed in as')}</p>
        <p className="mt-1.5 break-all text-base font-semibold leading-6">{email}</p>
        <Button variant="outline" className="mt-4 w-full rounded-full">{t('在此设备退出', 'Sign out on this device')}</Button>
      </section>
      <LicensesEntry />
      <div className="pt-10">
        <Button variant="link" className="h-auto px-0 text-destructive-ink">{t('删除账号', 'Delete account')}<ExternalLink className="size-4" /></Button>
        <p className="mt-2 text-xs leading-5 text-muted">{t('前往 avibe.bot，查看影响并确认删除。', 'Opens avibe.bot to review what is deleted and confirm.')}</p>
      </div>
    </>
  );
}

function AccountHeader() {
  const t = useCopy();
  return (
    <header className="shrink-0 px-3 pt-2 pb-4">
      <Button variant="ghost" size="sm" className="gap-1 px-2 text-mint-ink"><ChevronLeft className="size-4" />{t('实例', 'Instances')}</Button>
      <h2 className="mt-2 px-2 text-xl font-semibold tracking-tight">{t('账户', 'Account')}</h2>
    </header>
  );
}

export function SheetAccount({ current, others, email, swiping = false }: { current: SheetInstance; others: SheetInstance[]; email: string; swiping?: boolean }) {
  const t = useCopy();
  return (
    <Underneath>
      {swiping && (
        <SheetCard header={<SheetHeader current={current.name} />}>
          <CurrentInstance instance={current} />
          <OtherInstances instances={others} />
        </SheetCard>
      )}
      <SheetCard header={<AccountHeader />} className={swiping ? 'translate-x-1/3 shadow-elevation-3' : undefined}>
        <AccountPageBody email={email} />
      </SheetCard>
      {swiping && (
        <GestureNote className="top-1/2 left-4">
          <ArrowRight className="size-4" />{t('从左边缘右滑，回到实例列表', 'Swipe from the left edge to go back')}
        </GestureNote>
      )}
    </Underneath>
  );
}

export type FirstRunState = 'default' | 'pulling' | 'error';

/** First use, nothing selected yet: the same rows as a full page. Pull to refresh only here. */
export function FirstRunPage({ state, instances, email }: { state: FirstRunState; instances: SheetInstance[]; email: string }) {
  const t = useCopy();
  return (
    <div className="flex h-full flex-col overflow-hidden text-foreground" style={{ background: 'var(--gradient-wizard-platforms)' }}>
      <header className="flex h-[calc(2.75rem+env(safe-area-inset-top))] shrink-0 items-center justify-between px-5 pt-[env(safe-area-inset-top)]">
        <span className="flex items-center gap-2.5"><img src={logo} alt="" className="size-8" /><span className="text-lg font-semibold tracking-tight">Avibe</span></span>
        <Button variant="ghost" size="icon" className="rounded-full" aria-label={t(`账户 ${email}`, `Account ${email}`)}>
          <span className={accountHeaderAvatarClassName} aria-hidden="true">{accountInitialFor(email)}</span>
        </Button>
      </header>
      {state === 'pulling' && (
        <p role="status" className="flex shrink-0 items-center justify-center gap-2 pt-3 text-sm text-muted"><ArrowDown className="size-4" />{t('松开即可刷新', 'Release to refresh')}</p>
      )}
      <div className={cn('min-h-0 flex-1 overflow-y-auto px-5 pb-[calc(env(safe-area-inset-bottom)+1.5rem)]', state === 'pulling' && 'translate-y-4')}>
        <div className="pt-6 pb-6">
          <h1 className="text-3xl font-semibold leading-tight tracking-tight">{t('想从哪里开始？', 'Where shall we begin?')}</h1>
          <p className="mt-3 text-sm leading-6 text-muted">{t('选一个实例打开。以后点右上角的头像就能切换。', 'Pick an instance to open. Later, the avatar at the top switches.')}</p>
        </div>
        {state === 'error' ? (
          <div role="alert" className={cn(group, 'px-5 py-7 text-center')}>
            <AlertTriangle className="mx-auto mb-3 size-6 text-gold-ink" />
            <p className="text-sm font-semibold">{t('没能载入实例列表', 'Couldn’t load your instances')}</p>
            <p className="mt-1.5 text-xs leading-5 text-muted">{t('再试一次；如果一直这样，检查一下网络。', 'Try again. If it keeps happening, check your connection.')}</p>
            <Button variant="outline" className="mt-4 rounded-full"><RotateCw className="size-4" />{t('重试', 'Retry')}</Button>
          </div>
        ) : (
          <div className={cn(group, 'divide-y divide-border')}>
            {instances.map((instance) => <InstanceListRow key={instance.id} instance={instance} />)}
          </div>
        )}
        <AddInstanceRow />
      </div>
    </div>
  );
}

/** The native screen when an instance fails to load, in the same words as the sheet. */
export function RuntimeFailed({ instance }: { instance: SheetInstance }) {
  const t = useCopy();
  return (
    <div className="flex h-full flex-col items-center justify-center bg-background px-8 pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] text-center text-foreground">
      <WifiOff className="mb-5 size-9 text-gold-ink" />
      <h1 className="text-xl font-semibold leading-tight">{t(`暂时连不上 ${instance.name}`, `Can’t reach ${instance.name}`)}</h1>
      <p className="mt-3 text-sm leading-6 text-muted">{t('运行它的电脑可能离线了，或者这台手机的网络有问题。', 'The computer running it may be offline, or this phone’s connection has a problem.')}</p>
      <p className="mt-2 break-all text-xs text-muted">{instance.host}</p>
      <div className="mt-8 flex w-full max-w-xs flex-col gap-3">
        <Button variant="brand" size="lg" className="rounded-full">{t('重试', 'Try again')}</Button>
        <Button variant="outline" size="lg" className="rounded-full">{t('其他实例', 'Other instances')}</Button>
      </div>
    </div>
  );
}

/** Variant B: the instance's own header names the instance; its name opens the sheet, the avatar opens Account. Same height as the shipped `MobileBrandHeader`, drawn over it. */
function NamedHeader({ name }: { name: string }) {
  const t = useCopy();
  return (
    <header className="absolute inset-x-0 top-0 z-50 flex h-[calc(2.75rem+env(safe-area-inset-top))] items-center justify-between gap-2 border-b border-border bg-background px-4 pt-[env(safe-area-inset-top)]">
      <button type="button" className="flex min-w-0 items-center gap-2 rounded-md" aria-label={t(`当前实例 ${name}，切换实例`, `Current instance ${name}, switch instance`)}>
        <img src={logo} alt="" className="size-6 shrink-0 rounded-md border border-mint/30 bg-mint/[0.08] object-cover" />
        <span className="truncate text-[13px] font-semibold">{name}</span>
        <ChevronDown className="size-3.5 shrink-0 text-muted" />
      </button>
      <span className="inline-flex size-11 shrink-0 items-center justify-center rounded-full">
        <span className={accountHeaderAvatarClassName} aria-hidden="true">M</span>
      </span>
    </header>
  );
}

export function NamedHome({ current }: { current: SheetInstance }) {
  return <div className="relative h-full">{home()}<NamedHeader name={current.name} /></div>;
}

