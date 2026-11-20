// I5 · a long Turn on the Lock Screen (ui/design: ios/live-activity/*). The Lock Screen, the Dynamic
// Island and the Live Activity are drawn by iOS from the `AvibeLiveActivity` extension; this kit mocks
// them with product tokens so the states and words can be reviewed. Only the in-App chip is Web UI.
//
// What a card shows (owner decision 2026-11-20): the Avibe mark, the session title, the Agent's display
// name, one of five states, and times. Never message text, tool names, file paths, or the instance name.
// The title and name are written on the phone when tracking starts; pushes carry only the state.
import type { ReactNode } from 'react';
import { Check, Clock3, Hand, LockKeyhole, Settings, Square, X } from 'lucide-react';
import clsx from 'clsx';

import logo from '@/assets/logo.png';
import { Button } from '@/components/ui/button';

import { useDraft } from './draft';

export type TurnPhase = 'working' | 'needsYou' | 'done' | 'failed' | 'stopped';

export interface TurnActivity {
  phase: TurnPhase;
  /** The Agent's display name, set on the phone when tracking starts. */
  agent: string;
  /** The session title as the conversation header shows it; absent for a Bot's session. */
  title?: string;
  /** Elapsed time as the system timer draws it (counts locally, no push needed). */
  elapsed: string;
  /** Total time, for an ended Turn. */
  took?: string;
  /** Past its stale date: the last confirmed state and when it was confirmed. */
  staleSince?: string;
}

function Mark({ size = 'size-7' }: { size?: string }) {
  return <img src={logo} alt="" className={clsx(size, 'shrink-0 rounded-full')} />;
}

function Glyph({ phase, stale, className }: { phase: TurnPhase; stale?: boolean; className?: string }) {
  if (stale) return <Clock3 className={clsx('text-muted', className)} />;
  switch (phase) {
    case 'working':
      return <span className={clsx('relative inline-flex items-center justify-center', className)}><span className="size-2.5 rounded-full bg-mint shadow-glow-dot-mint" /></span>;
    case 'needsYou':
      return <Hand className={clsx('text-gold-ink', className)} />;
    case 'done':
      return <Check className={clsx('text-mint-ink', className)} />;
    case 'failed':
      return <X className={clsx('text-destructive-ink', className)} />;
    case 'stopped':
      return <Square className={clsx('text-muted', className)} />;
  }
}

function useTurnCopy() {
  const t = useDraft();
  return (a: TurnActivity) => {
    if (a.staleSince) {
      return {
        status: t('可能不是最新', 'May be out of date'),
        detail: t(`${a.staleSince} 最后确认在处理，打开 Avibe 查看`, `Last confirmed working at ${a.staleSince}. Open Avibe to check.`),
      };
    }
    switch (a.phase) {
      case 'working':
        return { status: t('正在处理', 'Working'), detail: t('轻点打开对话', 'Tap to open the conversation') };
      case 'needsYou':
        return { status: t('等你处理', 'Needs you'), detail: t('打开 Avibe 回应', 'Open Avibe to respond') };
      case 'done':
        return { status: t('已完成', 'Done'), detail: t(`用时 ${a.took}，轻点查看回复`, `Took ${a.took}. Tap to read the reply.`) };
      case 'failed':
        return { status: t('没有完成', "Didn't finish"), detail: t('打开 Avibe 查看原因', 'Open Avibe to see why') };
      case 'stopped':
        return { status: t('已停止', 'Stopped'), detail: t(`运行 ${a.took}后停止`, `Stopped after ${a.took}`) };
    }
  };
}

const statusTone = (a: TurnActivity) =>
  a.staleSince
    ? 'text-muted'
    : a.phase === 'needsYou'
      ? 'text-gold-ink'
      : a.phase === 'failed'
        ? 'text-destructive-ink'
        : a.phase === 'stopped'
          ? 'text-muted'
          : 'text-mint-ink';

/** Title, Agent and state, as the card and the expanded island share them. A Bot's session has no
 *  title, so its Agent name takes the first line. */
function Heading({ activity, onBlack = false }: { activity: TurnActivity; onBlack?: boolean }) {
  const copy = useTurnCopy()(activity);
  const muted = onBlack ? 'text-white/60' : 'text-muted';
  const tone = onBlack && (activity.staleSince || activity.phase === 'stopped') ? 'text-white/60' : statusTone(activity);
  return (
    <div className="min-w-0 flex-1">
      <div className="truncate text-[15px] font-semibold leading-5">{activity.title ?? activity.agent}</div>
      {activity.title && <div className={clsx('truncate text-[13px] leading-[18px]', muted)}>{activity.agent}</div>}
      <div className="mt-1 flex items-start gap-1.5 text-[13px] leading-[18px]">
        <Glyph phase={activity.phase} stale={Boolean(activity.staleSince)} className={clsx('mt-0.5 size-3.5 shrink-0', onBlack && activity.staleSince && 'text-white/60')} />
        <span>
          <span className={clsx('font-semibold', tone)}>{copy.status}</span>
          <span className={muted}>{' · '}{copy.detail}</span>
        </span>
      </div>
    </div>
  );
}

/** The Lock Screen card: mark, title, Agent, state, time. */
export function LiveActivityCard({ activity }: { activity: TurnActivity }) {
  const running = activity.phase === 'working' || activity.phase === 'needsYou';
  return (
    <div className="flex items-start gap-3 rounded-[24px] bg-card/90 p-4 text-card-foreground shadow-elevation-1 backdrop-blur">
      <Mark size="size-9" />
      <Heading activity={activity} />
      {running && !activity.staleSince && (
        <div className="pt-0.5 font-mono text-[20px] font-semibold tabular-nums leading-none">{activity.elapsed}</div>
      )}
    </div>
  );
}

/** A Lock Screen: wallpaper, the clock, and what is listed under it. */
export function LockScreen({ children, time = '10:24' }: { children: ReactNode; time?: string }) {
  const t = useDraft();
  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-background text-foreground" style={{ background: 'var(--gradient-wizard-platforms)' }}>
      <div className="mx-auto mt-3 h-[37px] w-[126px] rounded-full bg-black" />
      <div className="mt-6 text-center text-[17px] font-semibold text-foreground/80">{t('11月20日 星期四', 'Thursday, November 20')}</div>
      <div className="text-center font-display text-[88px] font-semibold leading-none tracking-tight">{time}</div>
      <div className="mt-auto flex flex-col gap-2 px-3 pb-24">{children}</div>
      <div className="absolute inset-x-0 bottom-2 mx-auto h-[5px] w-[134px] rounded-full bg-foreground/80" />
    </div>
  );
}

/** A labelled specimen on a plain sheet, for comparing states side by side. */
export function Specimen({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="font-mono text-[11px] text-muted">{label}</div>
      {children}
    </div>
  );
}

export function SpecimenSheet({ children }: { children: ReactNode }) {
  return (
    <div className="h-full overflow-hidden p-6 text-foreground" style={{ background: 'var(--gradient-wizard-platforms)' }}>
      <div className="flex flex-col gap-5">{children}</div>
    </div>
  );
}

/** The compact island: Avibe on the left, time or outcome on the right. */
export function IslandCompact({ activity }: { activity: TurnActivity }) {
  const running = activity.phase === 'working' && !activity.staleSince;
  return (
    <div className="flex h-[37px] w-[250px] items-center justify-between rounded-full bg-black pr-3 pl-1.5">
      <Mark size="size-6" />
      {running ? (
        <span className="font-mono text-[14px] font-semibold tabular-nums text-mint">{activity.elapsed}</span>
      ) : (
        <Glyph phase={activity.phase} stale={Boolean(activity.staleSince)} className={clsx('size-4', activity.staleSince && 'text-white/60')} />
      )}
    </div>
  );
}

/** The minimal island, when another app's activity shares it. */
export function IslandMinimal({ activity }: { activity: TurnActivity }) {
  return (
    <div className="flex size-[37px] items-center justify-center rounded-full bg-black">
      <Glyph phase={activity.phase} stale={Boolean(activity.staleSince)} className="size-4" />
    </div>
  );
}

/** The expanded island, on a long press. The island is always the hardware's black, in either theme. */
export function IslandExpanded({ activity }: { activity: TurnActivity }) {
  const running = activity.phase === 'working' || activity.phase === 'needsYou';
  return (
    <div className="flex w-[370px] items-start gap-3 rounded-[44px] bg-black px-5 pt-4 pb-5 text-white">
      <Mark size="size-9" />
      <Heading activity={activity} onBlack />
      {running && !activity.staleSince && <div className="pt-0.5 font-mono text-[18px] font-semibold tabular-nums">{activity.elapsed}</div>}
    </div>
  );
}

export type TrackChipState = 'offer' | 'tracking' | 'systemOff';

/** The in-App entry, above the composer while a Turn runs (iOS App only, `liveActivities` capability). */
export function TrackChip({ state }: { state: TrackChipState }) {
  const t = useDraft();
  if (state === 'systemOff') {
    return (
      <div className="flex items-center gap-2 rounded-2xl border border-edge bg-surface/95 py-2 pr-2 pl-3 shadow-elevation-1">
        <LockKeyhole className="size-4 shrink-0 text-muted" />
        <span className="min-w-0 flex-1 text-[13px] leading-[18px] text-muted">{t('实时活动在系统设置里关着', 'Live Activities are off in Settings')}</span>
        <Button variant="outline" size="sm" className="shrink-0 gap-1.5 rounded-full"><Settings className="size-3.5" />{t('去设置', 'Settings')}</Button>
      </div>
    );
  }
  if (state === 'tracking') {
    return (
      <div className="flex items-center gap-2 rounded-full border border-mint/30 bg-mint-soft py-1.5 pr-1.5 pl-3 shadow-elevation-1">
        <LockKeyhole className="size-4 shrink-0 text-mint-ink" />
        <span className="min-w-0 flex-1 text-[13px] font-medium text-mint-ink">{t('已在锁屏上显示', 'Showing on the Lock Screen')}</span>
        <Button variant="ghost" size="sm" className="shrink-0 rounded-full">{t('停止显示', 'Stop')}</Button>
      </div>
    );
  }
  return (
    <div className="flex justify-end">
      <Button variant="outline" size="sm" className="gap-1.5 rounded-full bg-surface/95 shadow-elevation-1">
        <LockKeyhole className="size-3.5" />
        {t('在锁屏上显示进度', 'Show on Lock Screen')}
      </Button>
    </div>
  );
}
