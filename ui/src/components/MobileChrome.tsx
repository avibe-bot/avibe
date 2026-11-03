import React, { type CSSProperties } from 'react';
import { Link, NavLink, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Loader2, Mic } from 'lucide-react';

import { Button } from './ui/button';
import { InstallHint } from './InstallHint';
import { BotFace } from './workbench/bots/BotFace';
import type { BotAvatar as BotAvatarSpec } from '../lib/bots';
import logoImg from '../assets/logo.png';

// The mobile shell's chrome as views (props only), so AppShell keeps the data and
// design frames can draw them (ui/design/AGENTS.md).

const badgeLabel = (count: number) => (count > 99 ? '99+' : String(count));

export type ShellNavItem = {
  to?: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  match?: (pathname: string) => boolean;
  badge?: number;
  onClick?: () => void;
};

export const MobileNavLink: React.FC<{ item: ShellNavItem }> = ({ item }) => {
  const location = useLocation();
  const active = item.match ? item.match(location.pathname) : location.pathname === item.to;
  const Icon = item.icon;

  const className = clsx(
    'flex min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-lg px-1 py-2 text-[10px] transition-colors',
    active ? 'bg-mint/[0.08] text-mint-ink' : 'text-muted'
  );
  const inner = (
    <>
      {/* Fixed-height icon slot so every tab's icon row is the same height and
          centers on one line — the workbench circle no longer protrudes above
          its siblings. */}
      <span className="relative flex h-7 items-center justify-center">
        <Icon className="size-4" />
        {item.badge ? (
          <span className="absolute -right-2 -top-1.5 min-w-[14px] rounded-full bg-mint px-1 text-center font-mono text-[9px] font-bold leading-[14px] text-primary-foreground">
            {badgeLabel(item.badge)}
          </span>
        ) : null}
      </span>
      <span className="max-w-full truncate">{item.label}</span>
    </>
  );

  if (item.onClick) {
    return <button type="button" onClick={item.onClick} className={className}>{inner}</button>;
  }
  return <NavLink to={item.to ?? '#'} className={className}>{inner}</NavLink>;
};

export type CenterButton = { label: string; icon: React.ComponentType<{ className?: string }>; to?: string; onClick?: () => void };

/** Where a press on the center is (ui/design: mobile/main-bot-voice/*): resting, pressed (not
 *  yet a hold), recording, recording with the finger in the cancel zone, or released and settling. */
export type BotCenterPhase = 'idle' | 'pressed' | 'recording' | 'armed' | 'settling';

/** The pointer handlers a press-and-hold puts on the center (`useHoldToTalk().bind`). */
export type BotCenterHold = Pick<
  React.DOMAttributes<HTMLButtonElement>,
  'onPointerDown' | 'onPointerMove' | 'onPointerUp' | 'onPointerCancel' | 'onLostPointerCapture' | 'onContextMenu' | 'onClick'
> & { style?: CSSProperties };

/** The main Bot in the middle of the bar (ui/design: mobile/workbench-tab/team): its
 *  avatar lifted above the bar's edge, its unread count, its running dot, its name. A
 *  tap opens its conversation; with `hold`, pressing and holding speaks to it. */
export type BotCenter = {
  avatar: BotAvatarSpec | null;
  name: string;
  /** The accessible name: what a tap does. */
  label: string;
  unread: number;
  running: boolean;
  onClick: () => void;
  phase?: BotCenterPhase;
  hold?: BotCenterHold;
};

export const MobileBotCenter: React.FC<BotCenter> = ({ avatar, name, label, unread, running, onClick, phase = 'idle', hold }) => {
  const voice = phase === 'recording' || phase === 'armed' || phase === 'settling';
  return (
    <button
      type="button"
      {...hold}
      // A press the hold answered (its tap opens the conversation) spends the click that follows;
      // any other click — the keyboard's, or a press the hold left alone — opens it here.
      onClick={(event) => {
        hold?.onClick?.(event);
        if (!event.defaultPrevented) onClick();
      }}
      aria-label={label}
      className="flex min-w-0 flex-1 flex-col items-center justify-end gap-1 px-1 pb-2 text-2xs"
    >
      {/* Lifted out of the row, so the bar keeps its height and the avatar rises above its edge. */}
      <span
        className={clsx(
          'relative -mt-5 rounded-full bg-surface p-0.5 ring-2 transition',
          phase === 'idle' && (running ? 'shadow-glow-lg-mint ring-mint' : 'shadow-glow-md-mint ring-mint/45'),
          phase === 'pressed' && 'scale-95 shadow-glow-md-mint ring-mint',
          phase === 'recording' && 'scale-110 shadow-glow-xl-mint ring-4 ring-mint',
          phase === 'armed' && 'scale-105 opacity-70 ring-4 ring-destructive/70',
          phase === 'settling' && 'shadow-glow-lg-mint ring-mint',
        )}
      >
        {phase === 'recording' && <span className="absolute -inset-2 animate-ping rounded-full border-2 border-mint/50 motion-reduce:animate-none" aria-hidden="true" />}
        <BotFace avatar={avatar} name={name} size={50} unread={voice ? 0 : unread} running={phase === 'idle' && running} />
        {(phase === 'recording' || phase === 'armed') && (
          <span className={clsx('absolute -bottom-1 -right-1 grid size-5 place-items-center rounded-full text-primary-foreground', phase === 'armed' ? 'bg-destructive' : 'bg-mint')}>
            <Mic className="size-3" />
          </span>
        )}
        {phase === 'settling' && (
          <span className="absolute -bottom-1 -right-1 grid size-5 place-items-center rounded-full bg-surface text-mint-ink">
            <Loader2 className="size-3.5 animate-spin" />
          </span>
        )}
      </span>
      <span className={clsx('max-w-full truncate font-semibold', running || voice ? 'text-mint-ink' : 'text-foreground')}>{name}</span>
    </button>
  );
};

// Workbench mobile tabs flank a raised center: the main Bot for the Owner, a
// new-session action for everyone else.
export const MobileTabBar: React.FC<{ items: ShellNavItem[]; center?: CenterButton; bot?: BotCenter }> = ({ items, center, bot }) => {
  // No center action means a plain even row of tabs.
  if (!center && !bot) {
    return (
      <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface/96 px-2 pt-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] backdrop-blur md:hidden">
        <div className="flex items-end justify-between gap-1">
          {items.map((item) => <MobileNavLink key={item.to ?? item.label} item={item} />)}
        </div>
      </nav>
    );
  }
  const half = Math.ceil(items.length / 2);
  const left = items.slice(0, half);
  const right = items.slice(half);
  // While the main Bot is held the bar rises above the take's scrim, so the held avatar stays bright.
  const raised = bot?.phase !== undefined && bot.phase !== 'idle';
  return (
    <nav className={clsx('fixed inset-x-0 bottom-0 border-t border-border bg-surface/96 px-2 pt-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] backdrop-blur md:hidden', raised ? 'z-50' : 'z-40')}>
      <div className="flex items-end justify-between gap-1">
        {left.map((item) => <MobileNavLink key={item.to ?? item.label} item={item} />)}
        {bot ? <MobileBotCenter {...bot} /> : center && <NewSessionCenter center={center} />}
        {right.map((item) => <MobileNavLink key={item.to ?? item.label} item={item} />)}
      </div>
    </nav>
  );
};

const NewSessionCenter: React.FC<{ center: CenterButton }> = ({ center }) => {
  const CenterIcon = center.icon;
  return (
    <div className="flex flex-1 justify-center">
      {center.onClick ? (
        <Button
          type="button"
          variant="brand"
          onClick={center.onClick}
          aria-label={center.label}
          className="size-12 -translate-y-1 rounded-full p-0 shadow-[0_8px_20px_-4px_rgba(91,255,160,0.6)]"
        >
          <CenterIcon className="size-6" />
        </Button>
      ) : (
        <Button
          asChild
          variant="brand"
          className="size-12 -translate-y-1 rounded-full p-0 shadow-[0_8px_20px_-4px_rgba(91,255,160,0.6)]"
        >
          <Link to={center.to ?? '/'} aria-label={center.label}>
            <CenterIcon className="size-6" />
          </Link>
        </Button>
      )}
    </div>
  );
};

// The mobile brand header above every tabbed page: the logo home link, and on
// iOS Safari the Add-to-Home-Screen nudge.
export const MobileBrandHeader: React.FC = () => {
  const { t } = useTranslation();
  return (
    <header className="sticky top-0 z-40 flex h-[calc(4rem+env(safe-area-inset-top))] shrink-0 items-center justify-between gap-2 border-b border-border bg-background/92 px-4 pt-[env(safe-area-inset-top)] backdrop-blur md:hidden">
      <Link
        to="/"
        className="flex min-w-0 items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/60"
      >
        <img
          src={logoImg}
          alt={t('appShell.logoAlt')}
          className="size-6 shrink-0 rounded-md border border-mint/30 bg-mint/[0.08] object-cover"
        />
        <span className="truncate text-[13px] font-semibold">{t('appShell.title')}</span>
      </Link>
      {/* Right side: the Add-to-Home-Screen nudge (renders only on iOS Safari
          when not yet installed; null everywhere else). Version / language /
          theme / account live in the More tab. */}
      <div className="flex items-center gap-1.5">
        <InstallHint />
      </div>
    </header>
  );
};
