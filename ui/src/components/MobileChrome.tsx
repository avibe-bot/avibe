import React, { type CSSProperties } from 'react';
import { Link, NavLink, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ChevronDown, Loader2, Mic } from 'lucide-react';

import { Button } from './ui/button';
import { InstallHint } from './InstallHint';
import { accountHeaderAvatarClassName, accountInitialFor } from './AccountAvatar';
import { BotFace } from './workbench/bots/BotFace';
import type { BotAvatar as BotAvatarSpec } from '../lib/bots';
import { useAuthAccount } from '../lib/useAuthAccount';
import { nativeShowAccount, nativeShowInstances } from '../lib/native';
import { useNativeInstanceName } from '../lib/useNativeInstanceName';
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

  // A tab answers a press like any control (docs/plans/web-ui-motion.md M8); an unselected tab's
  // colour step is the wash a selected one rests on.
  const className = clsx(
    'press flex min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-lg px-1 py-2 text-[10px]',
    active ? 'bg-mint/[0.08] text-mint-ink' : 'text-muted active:bg-mint/[0.08]'
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
      className="flex min-w-0 flex-1 flex-col items-center justify-between gap-1 self-stretch px-1 pb-2 text-2xs"
    >
      {/* Lifted out of the row, so the bar keeps its height and the avatar rises above its edge by
          `--mobile-nav-center-rise` (past the bar's top padding and border), the room a sheet that
          ends at the bar keeps for it. The name stays level with the other tabs' labels below.
          The avatar fills the button to its ring: one circle, the Bot's face in it. A flex box,
          so the face sets its height alone: no line box (whose depth varies with the font)
          makes the ring taller than wide or the button taller than the tabs. */}
      <span
        className={clsx(
          'relative flex -mt-[calc(var(--mobile-nav-center-rise)+0.5rem+1px)] rounded-full bg-surface transition',
          // At rest it is lit the way a selected Bot is (`.bot-avatar-selected`): a bright fine
          // edge and the theme's halo; the running dot says it is working.
          phase === 'idle' && 'bot-avatar-selected',
          phase === 'pressed' && 'scale-95 shadow-glow-md-mint ring-2 ring-mint',
          phase === 'recording' && 'scale-110 shadow-glow-xl-mint ring-4 ring-mint',
          phase === 'armed' && 'scale-105 opacity-70 ring-4 ring-destructive/70',
          phase === 'settling' && 'shadow-glow-lg-mint ring-2 ring-mint',
        )}
      >
        {phase === 'recording' && <span className="absolute -inset-2 animate-ping rounded-full border-2 border-mint/50 motion-reduce:animate-none" aria-hidden="true" />}
        <BotFace avatar={avatar} name={name} size={54} unread={voice ? 0 : unread} running={phase === 'idle' && running} />
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

// The bar is exactly `--mobile-nav-height` tall, so whatever sits above it starts at its edge; its
// bottom padding (`--mobile-nav-pad-bottom`) is the home indicator's inset where a phone has one
// (ui/design: home/home/e-native-phone).
// `data-mobile-tab-bar`: a sheet that ends at that edge draws the bar above itself (index.css).
const BAR = 'fixed inset-x-0 bottom-0 h-(--mobile-nav-height) border-t border-border bg-surface/96 px-2 pt-2 pb-(--mobile-nav-pad-bottom) backdrop-blur md:hidden';

// Workbench mobile tabs flank a raised center: the main Bot for the Owner, a
// new-session action for everyone else.
export const MobileTabBar: React.FC<{ items: ShellNavItem[]; center?: CenterButton; bot?: BotCenter }> = ({ items, center, bot }) => {
  // No center action means a plain even row of tabs.
  if (!center && !bot) {
    return (
      <nav data-mobile-tab-bar className={clsx(BAR, 'z-40')}>
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
    <nav data-mobile-tab-bar className={clsx(BAR, raised ? 'z-50' : 'z-40')}>
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
          className="size-12 -translate-y-1 rounded-full p-0"
        >
          <CenterIcon className="size-6" />
        </Button>
      ) : (
        <Button
          asChild
          variant="brand"
          className="size-12 -translate-y-1 rounded-full p-0"
        >
          <Link to={center.to ?? '/'} aria-label={center.label}>
            <CenterIcon className="size-6" />
          </Link>
        </Button>
      )}
    </div>
  );
};

// The mobile brand header above every tabbed page: the logo home link, on iOS Safari
// the Add-to-Home-Screen nudge, and in the phone app the account avatar. 44px below the
// safe area, a native navigation bar's height and the chat header's, exactly one 44px
// touch target tall, with the page's 16px top inset below it, so the first section
// neither crowds the header nor floats far below it. The open tab names the page for
// assistive tech only; the tab bar shows it to everyone else
// (ui/design: home/home/e-native-phone).
const NativeHeaderAccountAction: React.FC = () => {
  const { t } = useTranslation();
  const { email, signingOut, signOut } = useAuthAccount();
  const accountLabel = email
    ? t('appShell.accountMenuLabel', { email })
    : t('appShell.openMobileAccount');

  const openAccount = async () => {
    if (signingOut) return;
    if (!await nativeShowAccount()) {
      await signOut();
    }
  };

  return (
    <button
      type="button"
      aria-label={accountLabel}
      title={accountLabel}
      disabled={signingOut}
      onClick={() => void openAccount()}
      className="inline-flex size-11 shrink-0 items-center justify-center rounded-full transition hover:bg-wash focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
    >
      <span className={accountHeaderAvatarClassName} aria-hidden="true">
        {accountInitialFor(email)}
      </span>
    </button>
  );
};

// In the phone app with `instancePresentation`, the header names the instance it shows; the name
// opens the shell's instances sheet, and the avatar stays for the account
// (ui/design: mobile-app/switcher/b-home). Older shells keep "Avibe" and the avatar.
const NativeInstanceName: React.FC<{ name: string }> = ({ name }) => {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      aria-label={t('appShell.switchInstanceLabel', { name })}
      onClick={() => void nativeShowInstances()}
      className="flex min-w-0 items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <img
        src={logoImg}
        alt=""
        className="size-6 shrink-0 rounded-md border border-mint/30 bg-mint/[0.08] object-cover"
      />
      <span className="truncate text-sm font-semibold">{name}</span>
      <ChevronDown className="size-3.5 shrink-0 text-muted" aria-hidden="true" />
    </button>
  );
};

export const MobileBrandHeader: React.FC<{
  title?: string | null;
  nativeAccountAction?: boolean;
}> = ({ title, nativeAccountAction = false }) => {
  const { t } = useTranslation();
  const instanceName = useNativeInstanceName();
  return (
    <header className="sticky top-0 z-40 flex h-[calc(2.75rem+env(safe-area-inset-top))] shrink-0 items-center justify-between gap-2 border-b border-border bg-background/92 px-4 pt-[env(safe-area-inset-top)] backdrop-blur md:hidden">
      {title && <h1 className="sr-only">{title}</h1>}
      {nativeAccountAction && instanceName ? <NativeInstanceName name={instanceName} /> : <Link
        to="/"
        className="flex min-w-0 items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <img
          src={logoImg}
          alt={t('appShell.logoAlt')}
          className="size-6 shrink-0 rounded-md border border-mint/30 bg-mint/[0.08] object-cover"
        />
        <span className="truncate text-[13px] font-semibold">{t('appShell.title')}</span>
      </Link>}
      {/* Right side: the Add-to-Home-Screen nudge (renders only on iOS Safari
          when not yet installed; null everywhere else). Version / language /
          theme / account live in the More tab. */}
      <div className="flex items-center gap-1.5">
        <InstallHint />
        {nativeAccountAction && <NativeHeaderAccountAction />}
      </div>
    </header>
  );
};
