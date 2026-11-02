import React from 'react';
import { Link, NavLink, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Button } from './ui/button';
import { InstallHint } from './InstallHint';
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

// Workbench mobile tabs flank a raised new-session action.
export const MobileTabBar: React.FC<{ items: ShellNavItem[]; center?: CenterButton }> = ({ items, center }) => {
  // No center action means a plain even row of tabs.
  if (!center) {
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
  const CenterIcon = center.icon;
  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface/96 px-2 pt-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] backdrop-blur md:hidden">
      <div className="flex items-end justify-between gap-1">
        {left.map((item) => <MobileNavLink key={item.to ?? item.label} item={item} />)}
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
        {right.map((item) => <MobileNavLink key={item.to ?? item.label} item={item} />)}
      </div>
    </nav>
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
