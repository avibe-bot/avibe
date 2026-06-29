import React, { useEffect, useState } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { ArrowLeft, Bot, ChevronDown, FolderTree, Globe, Hash, Inbox, LayoutDashboard, LayoutGrid, Link as LinkIcon, Menu, MessageCircle, MonitorPlay, PlugZap, Plus, Settings, Sparkles, X } from 'lucide-react';
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
import { AppsLauncher } from './AppsLauncher';
import { WindowManagerProvider, useWindowManager } from '../context/WindowManagerContext';
import { WindowLayer } from './apps/WindowLayer';
import { NewSessionSheet } from './workbench/NewSessionSheet';
import { SearchPalette } from './workbench/search/SearchPalette';
import { Button } from './ui/button';
import { InstallHint } from './InstallHint';
import logoImg from '../assets/logo.png';
import { getEnabledPlatforms, platformSupportsChannels } from '../lib/platforms';
import { useViewportHeightVar } from '../lib/useViewportHeightVar';

type ShellNavItem = {
  // Optional: a parent that only groups children (no page of its own) omits `to`
  // and renders as a collapsible toggle instead of a link.
  to?: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  match?: (pathname: string) => boolean;
  badge?: number;
  children?: ShellNavItem[];
  // `defaultOpen` makes a group start expanded (used in the mobile 更多 sheet so
  // 通讯平台 shows its children without a second tap).
  defaultOpen?: boolean;
  // Mobile-tab extras: `onClick` makes the tab a button (e.g. 更多 opens the
  // nav sheet) instead of a link; `variant: 'workbench'` renders the emphasized
  // green circle for the back-to-workbench tab.
  onClick?: () => void;
  variant?: 'workbench';
};

const isItemActive = (item: ShellNavItem, pathname: string): boolean =>
  item.match
    ? item.match(pathname)
    : item.to
      ? pathname === item.to || pathname.startsWith(`${item.to}/`)
      : false;

// Mirrors design.pen kSWgv (VR/Sidebar): 240px width, fill --surface,
// right border, padding [20,16]. Mint-soft active state with mint glow.
const ShellNavLink: React.FC<{ item: ShellNavItem }> = ({ item }) => {
  const location = useLocation();
  if (item.children && item.children.length > 0) return <ShellNavGroup item={item} />;
  const active = item.match ? item.match(location.pathname) : location.pathname === item.to;
  const Icon = item.icon;

  return (
    <NavLink
      to={item.to ?? '#'}
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

// Collapsible parent for a nested submenu (e.g. 通讯平台 → 平台 / 群组 / 私聊).
// Auto-expands when one of its children is the active route; the parent has no
// page of its own, so it's a toggle button rather than a link.
const ShellNavGroup: React.FC<{ item: ShellNavItem }> = ({ item }) => {
  const location = useLocation();
  const Icon = item.icon;
  const childActive = (item.children ?? []).some((child) => isItemActive(child, location.pathname));
  const [open, setOpen] = useState(childActive || !!item.defaultOpen);
  useEffect(() => {
    if (childActive) setOpen(true);
  }, [childActive]);

  return (
    <div className="flex flex-col gap-0.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={clsx(
          'group flex w-full items-center gap-2.5 rounded-lg border border-transparent px-3 py-2.5 text-[13px] font-medium transition-colors hover:bg-foreground/[0.04]',
          childActive ? 'text-foreground' : 'text-muted hover:text-foreground'
        )}
      >
        <Icon className={clsx('size-4', childActive ? 'text-mint' : 'text-muted group-hover:text-foreground')} />
        <span className="flex-1 text-left">{item.label}</span>
        <ChevronDown className={clsx('size-3.5 shrink-0 text-muted transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="ml-3 flex flex-col gap-0.5 border-l border-border pl-2">
          {item.children!.map((child) => <ShellNavLink key={child.to} item={child} />)}
        </div>
      )}
    </div>
  );
};

const MobileNavLink: React.FC<{ item: ShellNavItem }> = ({ item }) => {
  const location = useLocation();
  const active = item.match ? item.match(location.pathname) : location.pathname === item.to;
  const Icon = item.icon;
  const isWorkbench = item.variant === 'workbench';

  const className = clsx(
    'flex min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-lg px-1 py-2 text-[10px] transition-colors',
    isWorkbench ? 'text-mint' : active ? 'bg-mint/[0.08] text-mint' : 'text-muted'
  );
  const inner = (
    <>
      {/* Fixed-height icon slot so every tab's icon row is the same height and
          centers on one line — the workbench circle no longer protrudes above
          its siblings. */}
      <span className="relative flex h-7 items-center justify-center">
        {isWorkbench ? (
          // Emphasized green circle — the back-to-workbench tab, mirroring the
          // desktop sidebar's distinct mint mode-switch button. Sized to fill the
          // slot so it sits on the same baseline as the plain icons.
          <span className="grid size-7 place-items-center rounded-full border border-mint/45 bg-mint/[0.14] shadow-[0_0_12px_-3px_rgba(91,255,160,0.6)]">
            <Icon className="size-4 text-mint" />
          </span>
        ) : (
          <Icon className="size-4" />
        )}
        {item.badge ? (
          <span className="absolute -right-2 -top-1.5 min-w-[14px] rounded-full bg-mint px-1 text-center font-mono text-[9px] font-bold leading-[14px] text-background">
            {item.badge > 99 ? '99+' : item.badge}
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

// When a window is MAXIMIZED it covers the sidebar (design If1Tt), so the in-sidebar Apps
// launcher is hidden behind it. This floats a second Apps launcher at the bottom-left, ABOVE the
// window layer, so the Dock stays reachable in full-screen — exactly the "Apps button floats on
// top" of If1Tt. It must live OUTSIDE the aside: the aside is `position: fixed`, which always
// forms a stacking context, so anything inside it can't rise above the window layer. Desktop-only
// (windows are md+). Only one Apps launcher is ever visible — the sidebar's is covered when this shows.
const FloatingApps: React.FC = () => {
  const { windows } = useWindowManager();
  const anyMaximized = windows.some((w) => w.maximized && !w.minimized);
  if (!anyMaximized) return null;
  return (
    // A solid rounded backing + shadow makes it read as a clean floating Dock launcher (design
    // If1Tt shows the pill clean) so the maximized app's content behind it — the editor activity
    // bar, the file-browser rail — doesn't bleed through the translucent pill. It follows the
    // workbench theme like the windows it sits over (no longer forced dark).
    <div
      className="fixed bottom-5 left-4 z-30 hidden w-[184px] rounded-full bg-surface-3 shadow-[0_10px_34px_-8px_rgba(0,0,0,0.7)] md:flex"
    >
      <AppsLauncher />
    </div>
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
  // The mobile admin nav sheet (opened from the 更多 tab). Close it whenever the
  // route changes so tapping any item in the sheet dismisses it.
  const [adminMenuOpen, setAdminMenuOpen] = useState(false);
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

  // Close the mobile admin nav sheet on any route change (tapping an item in it
  // navigates, which should dismiss the sheet).
  useEffect(() => {
    setAdminMenuOpen(false);
  }, [location.pathname]);

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
    { to: '/admin/remote-access', label: t('nav.remoteAccess'), icon: Globe },
    {
      // 通讯平台: groups everything about connecting messaging platforms — the
      // platform credentials (was a Settings tab), plus the group + DM scopes.
      label: t('nav.messagingPlatforms'),
      icon: LinkIcon,
      match: (p) =>
        p.startsWith('/admin/settings/platforms') ||
        p.startsWith('/admin/groups') ||
        p.startsWith('/admin/users'),
      children: [
        { to: '/admin/settings/platforms', label: t('settings.tabs.platforms'), icon: PlugZap },
        ...(hasChannelPlatforms ? [{ to: '/admin/groups', label: t('nav.channels'), icon: Hash }] : []),
        { to: '/admin/users', label: t('nav.users'), icon: MessageCircle },
      ],
    },
    {
      to: '/admin/settings/backends',
      label: t('nav.backends'),
      icon: Bot,
      match: (p) => p.startsWith('/admin/settings/backends'),
    },
    { to: '/admin/show-pages', label: t('nav.showPages'), icon: MonitorPlay },
    {
      // 高级设置: the remaining Settings tabs (messaging leads). Platforms +
      // backends moved out to their own sidebar destinations above, so exclude
      // their routes from the active match.
      to: '/admin/settings/messaging',
      label: t('nav.advancedSettings'),
      icon: Settings,
      match: (p) =>
        p.startsWith('/admin/settings') &&
        !p.startsWith('/admin/settings/platforms') &&
        !p.startsWith('/admin/settings/backends'),
    },
  ];

  const items: ShellNavItem[] = shellMode === 'admin' ? adminItems : [];

  // A bottom tab bar can't hold the nested admin nav (6 sections, one with a
  // submenu), so mobile keeps a trimmed 4-tab bar — back-to-workbench (emphasized
  // green circle), 控制台, 菜单 (opens the full nested nav sheet below), 高级设置 —
  // and the 菜单 sheet renders the same nested adminItems so every page is
  // reachable + groups expand. See ``adminMenuOpen``.
  const adminMobileTabs: ShellNavItem[] = [
    { to: '/', label: t('nav.workbench'), icon: Sparkles, variant: 'workbench' },
    { to: '/admin/dashboard', label: t('nav.dashboard'), icon: LayoutDashboard },
    { label: t('nav.more'), icon: Menu, onClick: () => setAdminMenuOpen(true), match: () => adminMenuOpen },
    {
      to: '/admin/settings/messaging',
      label: t('nav.advancedSettings'),
      icon: Settings,
      match: (p) =>
        p.startsWith('/admin/settings') &&
        !p.startsWith('/admin/settings/platforms') &&
        !p.startsWith('/admin/settings/backends'),
    },
  ];
  // The 更多 sheet shows the OVERFLOW — admin sections not already on the bottom
  // bar (控制台 + 高级设置) — so nothing is duplicated.
  const adminBottomBarPaths = new Set(['/admin/dashboard', '/admin/settings/messaging']);
  const adminSheetItems = adminItems
    .filter((item) => !item.to || !adminBottomBarPaths.has(item.to))
    // Groups start expanded in the sheet (the sheet is transient — show the
    // children up front). The desktop sidebar keeps its collapse-by-default.
    .map((item) => (item.children ? { ...item, defaultOpen: true } : item));

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
    <WindowManagerProvider>
    <div className="flex h-[var(--app-shell-h)] flex-col overflow-hidden bg-background text-foreground md:block md:h-auto md:min-h-screen md:overflow-visible">
      {/* No z-index on the aside itself: a maximized window (window layer, z-20) must be able to
          cover the sidebar nav (design If1Tt). Keeping it un-stacked (z-auto, no stacking context)
          lets the Apps launcher inside escape to its own z-30 and float on top while the rest of the
          sidebar stays below the window layer. */}
      <aside className="fixed inset-y-0 left-0 hidden w-[240px] flex-col border-r border-border bg-surface md:flex">
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

          {/* Bottom (design.pen NbPMq): row 1 = [Apps | Settings] two equal
              buttons; row 2 = [version … run-dot]. Admin keeps its quick-toggles
              + hostname between the rows. Only the Apps launcher floats above a
              maximized window (it carries its own z-30); Settings / version / run-dot
              stay at the sidebar's level and are covered by a maximized window (If1Tt). */}
          <div className="relative flex flex-col gap-3">
            {/* Row 1 — Apps (Dock trigger, left) paired with the mode switch
                (right). The Dock rises ABOVE the Apps button, clear of the
                centered Chat composer. Workbench → Settings (control panel);
                Control Panel → Back to Workbench, the mint counterpart. */}
            <div className="flex items-stretch gap-2">
              <AppsLauncher />
              {shellMode === 'workbench' ? (
                <Link
                  to="/admin/dashboard"
                  title={t('appShell.openControlPanel')}
                  aria-label={t('appShell.openControlPanel')}
                  className="group flex w-11 shrink-0 items-center justify-center rounded-lg border border-border-strong text-foreground transition-colors hover:bg-foreground/[0.04]"
                >
                  <Settings className="size-[18px] text-muted group-hover:text-foreground" />
                </Link>
              ) : (
                <Link
                  to="/"
                  className="flex flex-1 items-center justify-center gap-2 rounded-lg border border-mint/30 bg-mint/[0.06] px-3 py-2.5 text-[13px] font-semibold text-mint transition hover:bg-mint/[0.12]"
                >
                  <ArrowLeft className="size-3.5" />
                  <span>{t('appShell.backToWorkbench')}</span>
                </Link>
              )}
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

            {/* Row 2 (design bVke5) — run-state dot + label on the LEFT, version on the RIGHT. */}
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 text-[11px] font-medium text-muted">
                <span
                  className={clsx(
                    'size-2 shrink-0 rounded-full',
                    isRunning ? 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.9)]' : 'bg-muted'
                  )}
                />
                {isRunning ? t('common.running') : t('common.stopped')}
              </span>
              <VersionBadge openUpward />
            </div>
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
          <MobileTabBar items={adminMobileTabs} />
        ) : (
          <MobileTabBar
            items={workbenchTabs}
            center={{ onClick: () => setNewSessionOpen(true), label: t('appShell.newSession'), icon: Plus }}
          />
        )
      )}

      {/* Mobile admin nav sheet — the full nested adminItems (groups expand),
          opened from the 更多 tab. Mounted only in the admin shell on mobile. */}
      {shellMode === 'admin' && adminMenuOpen && (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true">
          <button
            type="button"
            aria-label={t('common.close')}
            onClick={() => setAdminMenuOpen(false)}
            className="absolute inset-0 bg-background/70 backdrop-blur-sm"
          />
          {/* Floats as a card ABOVE the bottom tab bar (not flush to the screen
              edge) so the list sits clear of the nav and the thumb-tap zone. */}
          <div className="absolute inset-x-2 bottom-[calc(4.5rem+env(safe-area-inset-bottom))] max-h-[68vh] overflow-y-auto rounded-2xl border border-border bg-surface px-3 pb-3 pt-1 shadow-2xl">
            <div className="relative flex items-center justify-center py-2">
              <span className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
                {t('appShell.moreSettings')}
              </span>
              <button
                type="button"
                aria-label={t('common.close')}
                onClick={() => setAdminMenuOpen(false)}
                className="absolute right-1 top-1.5 grid size-8 place-items-center rounded-lg text-muted transition-colors hover:bg-foreground/[0.06] hover:text-foreground"
              >
                <X className="size-4" />
              </button>
            </div>
            <nav className="flex flex-col gap-0.5 pb-2">
              {adminSheetItems.map((item) => <ShellNavLink key={item.to ?? item.label} item={item} />)}
            </nav>
          </div>
        </div>
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

      {/* App windows float over the workbench main area (desktop). The Dock (P2)
          and the AppsLauncher bridge open windows via the WindowManager. */}
      <WindowLayer />
      {/* The Apps launcher floats back on top when a window is maximized (If1Tt). */}
      <FloatingApps />
    </div>
    </WindowManagerProvider>
  );
};
