import React, { useEffect, useState } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { ArrowLeft, ArrowRight, FolderTree, Hash, Inbox, LayoutDashboard, LayoutGrid, Menu, MonitorPlay, Plus, Settings, SlidersHorizontal, Sparkles, Users } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi } from '../context/ApiContext';
import { useStatus } from '../context/StatusContext';
import { useWorkbenchInbox } from '../context/WorkbenchInboxContext';
import { AccountMenu } from './AccountMenu';
import { LanguageSwitcher } from './LanguageSwitcher';
import { ThemeToggle } from './ThemeToggle';
import { VersionBadge } from './VersionBadge';
import { WorkbenchSidebar } from './workbench/WorkbenchSidebar';
import { NewSessionSheet } from './workbench/NewSessionSheet';
import { SearchPalette } from './workbench/search/SearchPalette';
import { Button } from './ui/button';
import { InstallHint } from './InstallHint';
import logoImg from '../assets/logo.png';
import { getEnabledPlatforms, platformSupportsChannels } from '../lib/platforms';
import { useViewportHeightVar } from '../lib/useViewportHeightVar';

type ShellNavItem = {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  match?: (pathname: string) => boolean;
  badge?: number;
};

// Mirrors design.pen kSWgv (VR/Sidebar): 240px width, fill --surface,
// right border, padding [20,16]. Mint-soft active state with mint glow.
const ShellNavLink: React.FC<{ item: ShellNavItem }> = ({ item }) => {
  const location = useLocation();
  const active = item.match ? item.match(location.pathname) : location.pathname === item.to;
  const Icon = item.icon;

  return (
    <NavLink
      to={item.to}
      className={clsx(
        'group flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-[13px] font-medium transition-colors',
        active
          ? 'border border-mint/30 bg-mint/[0.08] text-foreground shadow-[0_0_16px_-4px_rgba(91,255,160,0.5)]'
          : 'border border-transparent text-muted hover:bg-foreground/[0.04] hover:text-foreground'
      )}
    >
      <Icon className={clsx('size-4', active ? 'text-mint' : 'text-muted group-hover:text-foreground')} />
      <span>{item.label}</span>
    </NavLink>
  );
};

const MobileNavLink: React.FC<{ item: ShellNavItem }> = ({ item }) => {
  const location = useLocation();
  const active = item.match ? item.match(location.pathname) : location.pathname === item.to;
  const Icon = item.icon;

  return (
    <NavLink
      to={item.to}
      className={clsx(
        'flex min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-lg px-1 py-2 text-[10px] transition-colors',
        active ? 'bg-mint/[0.08] text-mint' : 'text-muted'
      )}
    >
      <span className="relative">
        <Icon className="size-4" />
        {item.badge ? (
          <span className="absolute -right-2 -top-1.5 min-w-[14px] rounded-full bg-mint px-1 text-center font-mono text-[9px] font-bold leading-[14px] text-background">
            {item.badge > 99 ? '99+' : item.badge}
          </span>
        ) : null}
      </span>
      <span className="max-w-full truncate">{item.label}</span>
    </NavLink>
  );
};

type CenterButton = { label: string; icon: React.ComponentType<{ className?: string }>; to?: string; onClick?: () => void };

// Mobile bottom tab bar shared by both shells. Section tabs flank a raised
// center FAB. Workbench: center = ＋ (new session). Control Panel: center =
// Workbench (jump back) — the symmetric counterpart Alex asked for, so each
// shell can reach the other from the tab bar.
const MobileTabBar: React.FC<{ items: ShellNavItem[]; center?: CenterButton }> = ({ items, center }) => {
  // No center FAB → a plain even row of tabs. The Control Panel uses this so
  // "Workbench" is just the first tab, which reads cleaner than an asymmetric
  // raised center button.
  if (!center) {
    return (
      <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface/96 px-2 pt-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] backdrop-blur md:hidden">
        <div className="flex items-end justify-between gap-1">
          {items.map((item) => <MobileNavLink key={item.to} item={item} />)}
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
        {left.map((item) => <MobileNavLink key={item.to} item={item} />)}
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
        {right.map((item) => <MobileNavLink key={item.to} item={item} />)}
      </div>
    </nav>
  );
};

export const AppShell: React.FC = () => {
  const { t } = useTranslation();
  const { status } = useStatus();
  const { totalUnread } = useWorkbenchInbox();
  const api = useApi();
  const location = useLocation();
  const [enabledPlatforms, setEnabledPlatforms] = useState<string[]>([]);
  const [config, setConfig] = useState<any>(null);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  // Mirror the iOS visual-viewport height into --app-vvh. The MOBILE shell is a
  // static locked column that does NOT read it (resizing the shell mid-focus
  // fought iOS's scroll-into-view and flung the input off-screen); only the md+
  // chat (iPad / phone-landscape — desktop layout, so it can't use the mobile
  // body-lock) sizes to it, keeping its composer above the soft keyboard.
  useViewportHeightVar();

  useEffect(() => {
    api.getConfig().then((c: any) => {
      setConfig(c);
      setEnabledPlatforms(getEnabledPlatforms(c));
    }).catch(() => {});
  }, [api]);

  // Global ⌘K / Ctrl+K toggles the message-search palette. Intercept the chord
  // everywhere (it's a deliberate command, so it wins even from the composer);
  // the palette's own input/Esc/arrow handling takes over once it is open.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setSearchOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  const hasChannelPlatforms = enabledPlatforms.some((platform) => platformSupportsChannels(config, platform));
  const isRunning = status.state === 'running';

  if (location.pathname === '/setup') {
    return <Outlet />;
  }

  // Two shell modes share the same chrome (brand + bottom status):
  //   - admin: control-panel pages under /admin/* (legacy dashboard/groups/...
  //     paths are now Navigate redirects to /admin/*).
  //   - workbench: the new `/` entry. Commit 01 ships a placeholder with no
  //     sidebar nav; commit 02 layers in the capability modules + projects.
  const shellMode: 'workbench' | 'admin' =
    location.pathname.startsWith('/admin') ? 'admin' : 'workbench';

  const adminItems: ShellNavItem[] = [
    { to: '/admin/dashboard', label: t('nav.dashboard'), icon: LayoutDashboard },
    ...(hasChannelPlatforms ? [{ to: '/admin/groups', label: t('nav.channels'), icon: Hash }] : []),
    { to: '/admin/users', label: t('nav.users'), icon: Users },
    { to: '/admin/show-pages', label: t('nav.showPages'), icon: MonitorPlay },
    {
      to: '/admin/settings/service',
      label: t('nav.settings'),
      icon: Settings,
      match: (pathname) => pathname.startsWith('/admin/settings'),
    },
  ];

  const items: ShellNavItem[] = shellMode === 'admin' ? adminItems : [];

  // Workbench mobile tabs flatten the (desktop-only) WorkbenchSidebar into a
  // bottom tab bar: Inbox / Projects / Capabilities / More, around a center
  // ＋ that opens the workbench canvas (new session). Capabilities routes to
  // Agents and stays active across the four capability pages.
  const workbenchTabs: ShellNavItem[] = [
    { to: '/inbox', label: t('nav.inbox'), icon: Inbox, badge: totalUnread },
    { to: '/projects', label: t('nav.projects'), icon: FolderTree },
    {
      to: '/agents',
      label: t('nav.capabilities'),
      icon: LayoutGrid,
      match: (p) => ['/agents', '/skills', '/harness', '/vaults'].some((x) => p.startsWith(x)),
    },
    { to: '/more', label: t('nav.more'), icon: Menu, match: (p) => p.startsWith('/more') },
  ];

  // Chat is a full-screen detail (own composer) and Search is a full-screen
  // focused surface (own header + back button); the wizard owns the whole
  // viewport. These mobile surfaces render their own top chrome, so the shell's
  // mobile brand header AND the bottom tab bar are hidden on them.
  const isChat = location.pathname.startsWith('/chat/');
  const isSearch = location.pathname === '/search';
  const isFullScreenMobile = isChat || isSearch;
  const showBottomNav = !isFullScreenMobile && location.pathname !== '/setup';

  return (
    // Mobile: a LOCKED, full-viewport flex column (overflow-hidden) so the
    // document never scrolls — iOS can't then fling a focused input off the top —
    // and <main> scrolls internally. The height is the STATIC --app-shell-h (dvh,
    // with a 100vh fallback for older iOS): we deliberately do NOT resize the shell
    // to the visual viewport in JS, because mutating the shell height mid-focus
    // fought iOS's own scroll-into-view and threw the input off-screen. iOS instead
    // pans the locked page to lift the focused composer above the keyboard.
    // Desktop: normal document flow.
    <div className="flex h-[var(--app-shell-h)] flex-col overflow-hidden bg-background text-foreground md:block md:h-auto md:min-h-screen md:overflow-visible">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-[240px] flex-col border-r border-border bg-surface md:flex">
        <div className="flex h-full flex-col justify-between gap-6 px-4 py-5">
          {/* Top: Brand + Workspace label + Nav list */}
          {/* Workbench mounts a search field right under the brand, so use the
              same gap as the sidebar's own rows (gap-4) for an even rhythm; admin
              keeps the wider gap-6 to separate the brand from its labelled nav. */}
          <div className={clsx('flex min-h-0 flex-1 flex-col', shellMode === 'workbench' ? 'gap-4' : 'gap-6')}>
            <div className="flex items-center gap-2.5 px-1 py-2">
              <img
                src={logoImg}
                alt="avibe logo"
                className="size-9 rounded-lg border border-mint/35 bg-mint/[0.08] object-cover shadow-[0_0_16px_-4px_rgba(91,255,160,0.5)]"
              />
              <div className="min-w-0">
                <div className="truncate text-[13px] font-semibold text-foreground">{t('appShell.title')}</div>
                <div className="truncate text-[11px] text-muted">{t('appShell.subtitle')}</div>
              </div>
            </div>

            {shellMode === 'admin' && items.length > 0 && (
              <div className="flex flex-col gap-2">
                <div className="px-1 font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
                  {t('appShell.workspaceLabel')}
                </div>
                <nav className="flex flex-col gap-0.5">
                  {items.map((item) => <ShellNavLink key={item.to} item={item} />)}
                </nav>
              </div>
            )}
            {shellMode === 'workbench' && <WorkbenchSidebar onOpenSearch={() => setSearchOpen(true)} />}
          </div>

          {/* Bottom: Status (with embedded version badge) + toggles + hostname */}
          <div className="flex flex-col gap-3">
            <div
              className={clsx(
                'flex items-center gap-2.5 rounded-lg border px-3 py-2.5',
                isRunning
                  ? 'border-mint/30 bg-mint/[0.08]'
                  : 'border-border bg-foreground/[0.02]'
              )}
            >
              <span
                className={clsx(
                  'size-2 shrink-0 rounded-full',
                  isRunning ? 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.9)]' : 'bg-muted'
                )}
              />
              <div className="min-w-0 flex-1">
                <div className="text-[12px] font-medium text-foreground">
                  {isRunning ? t('common.running') : t('common.stopped')}
                </div>
                <div className="text-[10px] text-muted">{t('appShell.statusLabel')}</div>
              </div>
              <VersionBadge openUpward />
            </div>

            {/* Language / theme / account quick-toggles only show in the
                Control Panel, which is the operational surface. The
                Workbench sidebar stays focused on the agent task itself;
                the same controls are reachable by switching modes. */}
            {shellMode === 'admin' && (
              <div className="flex items-center gap-2">
                <LanguageSwitcher openUpward />
                <ThemeToggle />
                <AccountMenu openUpward />
              </div>
            )}

            {config?.runtime?.hostname && (
              <div className="truncate font-mono text-[10px] text-muted">
                {config.runtime.hostname}
              </div>
            )}

            {/* Mode switch — flips between Workbench (`/`) and Control Panel
                (`/admin/*`). Distinct visual hierarchy from the toggle row
                above so users notice it as a destination, not a quick toggle. */}
            {shellMode === 'workbench' ? (
              <Link
                to="/admin/dashboard"
                className="flex items-center justify-center gap-2 rounded-lg border border-border-strong px-3 py-2.5 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
              >
                <SlidersHorizontal className="size-3.5" />
                <span>{t('appShell.openControlPanel')}</span>
                <ArrowRight className="size-3 text-muted" />
              </Link>
            ) : (
              <Link
                to="/"
                className="flex items-center justify-center gap-2 rounded-lg border border-mint/30 bg-mint/[0.06] px-3 py-2.5 text-[12px] font-semibold text-mint transition hover:bg-mint/[0.12]"
              >
                <ArrowLeft className="size-3.5" />
                <span>{t('appShell.backToWorkbench')}</span>
              </Link>
            )}
          </div>
        </div>
      </aside>

      {/* Chat and Search are fixed full-screen surfaces with their own header
          bars, so the brand header is hidden there (otherwise it would sit
          behind them). */}
      {!isFullScreenMobile && (
        <header className="sticky top-0 z-40 flex h-[calc(4rem+env(safe-area-inset-top))] shrink-0 items-center justify-between gap-2 border-b border-border bg-background/92 px-4 pt-[env(safe-area-inset-top)] backdrop-blur md:hidden">
          <div className="flex min-w-0 items-center gap-2">
            <img
              src={logoImg}
              alt="avibe logo"
              className="size-6 shrink-0 rounded-md border border-mint/30 bg-mint/[0.08] object-cover"
            />
            <span className="truncate text-[13px] font-semibold">{t('appShell.title')}</span>
          </div>
          {/* Right side: the Add-to-Home-Screen nudge (renders only on iOS Safari
              when not yet installed; null everywhere else). Version / language /
              theme / account live in the More tab. */}
          <InstallHint />
        </header>
      )}

      <main
        className={clsx(
          // Mobile: the internal scroll area of the locked flex-column shell, so
          // the document itself never scrolls. Desktop: normal flow (min-h-screen
          // + sidebar offset).
          'flex-1 min-h-0 overflow-y-auto md:ml-[240px] md:min-h-screen md:flex-none md:overflow-visible md:pb-0',
          showBottomNav ? 'pb-[calc(5.5rem+env(safe-area-inset-bottom))]' : 'pb-0',
          location.pathname.startsWith('/admin/settings') ? 'page-glow-settings' : 'page-glow-console'
        )}
      >
        <div className="mx-auto w-full px-4 py-5 md:px-10 md:py-8">
          <Outlet />
        </div>
      </main>

      {showBottomNav && (
        shellMode === 'admin' ? (
          <MobileTabBar
            items={[{ to: '/', label: t('nav.workbench'), icon: Sparkles }, ...adminItems]}
          />
        ) : (
          <MobileTabBar
            items={workbenchTabs}
            center={{ onClick: () => setNewSessionOpen(true), label: t('appShell.newSession'), icon: Plus }}
          />
        )
      )}

      <NewSessionSheet
        open={newSessionOpen}
        onClose={() => setNewSessionOpen(false)}
        onOpen={() => setNewSessionOpen(true)}
      />

      {/* ⌘K message-search palette. Mounted shell-wide so the shortcut works from
          both Workbench and Control Panel; the sidebar field is the workbench
          entry point. */}
      <SearchPalette open={searchOpen} onClose={() => setSearchOpen(false)} />
    </div>
  );
};
