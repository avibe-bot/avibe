import React, { useEffect, useState } from 'react';
import { Link, Navigate, NavLink, Outlet, useLocation } from 'react-router-dom';
import { AlertTriangle, ArrowLeft, Bot, Brain, Building2, ChevronDown, Cpu, FolderTree, Globe, Grid2x2, Hash, Inbox, LayoutDashboard, LayoutGrid, Link as LinkIcon, Menu, MessageCircle, PlugZap, Plus, Settings, Sparkles, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { APP_TAB_PARAM, isStandaloneAppRoutePath, isStandaloneAppTab } from '../apps/appLaunch';
import { StandaloneAppTabContext } from '../context/StandaloneAppTabContext';
import { modelHubEnabledFromConfig } from './settings/models/featureFlags';
import { memoryNavShouldBeVisible } from '../lib/memorySettings';
import { useApi } from '../context/ApiContext';
import { useStatus } from '../context/StatusContext';
import { useWorkbenchInbox } from '../context/WorkbenchInboxContext';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { AccountMenu } from './AccountMenu';
import { LanguageSwitcher } from './LanguageSwitcher';
import { ThemeToggle } from './ThemeToggle';
import { VersionBadge } from './VersionBadge';
import { WorkbenchSidebar } from './workbench/WorkbenchSidebar';
import { AppsLauncher } from './AppsLauncher';
import { ErrorBoundary } from './ui/error-boundary';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';
import { WindowManagerProvider } from '../context/WindowManagerProvider';
import { DockProvider } from '../context/DockProvider';
import { ShowPageDragProvider } from '../context/ShowPageDragProvider';
import { WindowLayer } from './apps/WindowLayer';
import { MobileDockDrawer } from './apps/MobileDockDrawer';
import { NewSessionSheet } from './workbench/NewSessionSheet';
import { SearchPalette } from './workbench/search/SearchPalette';
import { Button } from './ui/button';
import { InstallHint } from './InstallHint';
import logoImg from '../assets/logo.png';
import { getEnabledPlatforms, platformSupportsChannels } from '../lib/platforms';
import { useViewportHeightVar } from '../lib/useViewportHeightVar';
import {
  adminLandingPath,
  isAdvancedSettingsPath,
  isOwnerOnlyPath,
  isMemorySettingsPath,
  visibleAdminNavItems,
} from '../lib/adminNavigation';

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
      <Icon className={clsx('size-4', active ? 'text-mint-ink' : 'text-muted group-hover:text-foreground')} />
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
        <Icon className={clsx('size-4', childActive ? 'text-mint-ink' : 'text-muted group-hover:text-foreground')} />
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
    isWorkbench ? 'text-mint-ink' : active ? 'bg-mint/[0.08] text-mint-ink' : 'text-muted'
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
            <Icon className="size-4 text-mint-ink" />
          </span>
        ) : (
          <Icon className="size-4" />
        )}
        {item.badge ? (
          <span className="absolute -right-2 -top-1.5 min-w-[14px] rounded-full bg-mint px-1 text-center font-mono text-[9px] font-bold leading-[14px] text-primary-foreground">
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

type ConfigRecoveryProjection = {
  config_recovery?: {
    required?: boolean;
    warnings?: unknown[];
  };
};

const ConfigRecoveryNotice: React.FC<{ config: ConfigRecoveryProjection | null }> = ({ config }) => {
  const { t } = useTranslation();
  if (!config?.config_recovery?.required) return null;

  return (
    <div className="fixed inset-x-2 top-2 z-[70] mx-auto flex max-w-3xl items-start gap-3 rounded-lg border border-gold/45 bg-surface px-3 py-2.5 shadow-xl" role="alert">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold-ink" />
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold text-foreground">{t('configRecovery.title')}</p>
        <p className="mt-0.5 break-words text-[12px] text-muted">
          {t('configRecovery.body')}
        </p>
      </div>
      <Link
        to="/admin/settings/diagnostics"
        className="shrink-0 text-[12px] font-semibold text-gold-ink hover:underline"
      >
        {t('configRecovery.action')}
      </Link>
    </div>
  );
};

export const AppShell: React.FC = () => {
  const { t } = useTranslation();
  const { status } = useStatus();
  const { totalUnread } = useWorkbenchInbox();
  const {
    capabilities,
    instanceKind,
    remote,
  } = useInstanceAuthorization();
  const api = useApi();
  const location = useLocation();
  const [enabledPlatforms, setEnabledPlatforms] = useState<string[]>([]);
  const [config, setConfig] = useState<any>(null);
  const [memoryNavVisible, setMemoryNavVisible] = useState(false);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  // The mobile admin nav sheet (opened from the 更多 tab). Close it whenever the
  // route changes so tapping any item in the sheet dismisses it.
  const [adminMenuOpen, setAdminMenuOpen] = useState(false);
  // The mobile Dock drawer (opened from the workbench Apps tab). Like the admin
  // sheet it closes on any route change — tapping a tile navigates + dismisses.
  const [appsDrawerOpen, setAppsDrawerOpen] = useState(false);
  // Whether this DOCUMENT was opened as a single-app tab (⌘/Ctrl-click on an app icon,
  // §7.1m). Frozen at mount from the landing URL rather than tracked off `location`, so
  // navigating deeper inside the tab can't suddenly restore the workbench window layout
  // this tab exists to stay out of — nor re-enable the save that would clobber it.
  const [standaloneAppTab] = useState(() =>
    typeof window === 'undefined' ? false : isStandaloneAppTab(window.location.search),
  );
  // Mirror the iOS visual-viewport height into --app-vvh. The MOBILE shell is a
  // static locked column that does NOT read it (resizing the shell mid-focus
  // fought iOS's scroll-into-view and flung the input off-screen); only the md+
  // chat (iPad / phone-landscape — desktop layout, so it can't use the mobile
  // body-lock) sizes to it, keeping its composer above the soft keyboard.
  useViewportHeightVar();

  useEffect(() => {
    if (!capabilities.can_manage_instance) return;
    api.getConfig().then((c: any) => {
      setConfig(c);
      setEnabledPlatforms(getEnabledPlatforms(c));
    }).catch(() => {});
  }, [api, capabilities.can_manage_instance]);

  useEffect(() => {
    const refreshMemoryNav = () => {
      void api.getMemorySettings()
        .then((memory) => setMemoryNavVisible(memoryNavShouldBeVisible(memory)))
        .catch(() => setMemoryNavVisible(false));
    };
    refreshMemoryNav();
    window.addEventListener('avibe:memory-settings-changed', refreshMemoryNav);
    return () => window.removeEventListener('avibe:memory-settings-changed', refreshMemoryNav);
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

  // Close the mobile transient surfaces (admin nav sheet + Apps Dock drawer) on
  // any route change — tapping an item in either navigates, which dismisses it.
  useEffect(() => {
    setAdminMenuOpen(false);
    setAppsDrawerOpen(false);
  }, [location.pathname]);

  // A single-app tab sitting on the app's own route (⌘/Ctrl-clicked Terminal / Files /
  // Editor, or that URL bookmarked): the tab exists to show ONE app, so it drops EVERY
  // piece of shell chrome — sidebar, mobile brand header, bottom tab bar, page padding —
  // and hands the whole viewport to the app. Both halves matter: the flag alone would
  // strip the chrome off any page such a tab later navigates to, and the route alone
  // would strip it inside the normal workbench.
  //
  // Route-scoped, and therefore LOCAL to the layout: what `StandaloneAppTabContext`
  // publishes is the mount-frozen document flag instead, because the window controls
  // that read it must stay decided for the tab's whole life (see the context doc). The
  // app pages read the same context and mount only on these routes, so for them the two
  // agree anyway.
  const chromeless = standaloneAppTab && isStandaloneAppRoutePath(location.pathname);

  // Keep the visible URL honest about standalone mode. An in-tab app-to-app navigation
  // (Files → "Open in Editor" / "Open Terminal Here") lands on `/apps/editor` WITHOUT the
  // marker, while the document is still the single-app tab — `standaloneAppTab` is frozen
  // at mount by design. Reloading, bookmarking, or copying that URL would otherwise bring
  // back the full workbench chrome and the restored window layout the tab exists to avoid.
  //
  // `history.replaceState` rather than a router navigate: this only corrects what the
  // address bar shows, and leaving the router's own location (and `location.key`) untouched
  // keeps launch effects keyed on it — the Terminal's "open one tab per launch" — from
  // firing a second time for the same navigation.
  useEffect(() => {
    if (!chromeless) return;
    const url = new URL(window.location.href);
    if (url.searchParams.get(APP_TAB_PARAM) === '1') return;
    url.searchParams.set(APP_TAB_PARAM, '1');
    window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
  }, [chromeless, location.pathname, location.search]);

  if (
    location.pathname === '/setup' &&
    remote &&
    capabilities.can_manage_instance
  ) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background p-4 text-foreground">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle>{t('setup.remoteOwner.title')}</CardTitle>
            <CardDescription>{t('setup.remoteOwner.body')}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm leading-relaxed text-muted">{t('setup.remoteOwner.hint')}</p>
            <Button asChild>
              <Link to={adminLandingPath(capabilities.can_use_system)}>
                {t('setup.remoteOwner.action')}
              </Link>
            </Button>
          </CardContent>
        </Card>
      </main>
    );
  }

  const hasChannelPlatforms = enabledPlatforms.some((platform) => platformSupportsChannels(config, platform));
  const modelHubEnabled = modelHubEnabledFromConfig(config);
  const isRunning = status.state === 'running';
  const canUseApps = capabilities.can_chat;
  const showOrganizationNavigation = instanceKind === 'organization';
  const canUseShowPageApp =
    location.pathname.startsWith('/apps/show/') && capabilities.can_use_show_pages;
  const localSystemPath = isOwnerOnlyPath(location.pathname);
  const resourceUseDenied =
    (location.pathname.startsWith('/agents') && !capabilities.can_use_agents) ||
    (location.pathname.startsWith('/harness') && !capabilities.can_chat) ||
    (location.pathname.startsWith('/skills') && !capabilities.can_use_skills) ||
    (location.pathname.startsWith('/vaults') && !capabilities.can_use_vault_secrets);
  const appAccessDenied = location.pathname.startsWith('/apps/') && !canUseApps && !canUseShowPageApp;
  if (
    (localSystemPath && !capabilities.can_manage_instance) ||
    resourceUseDenied ||
    appAccessDenied
  ) {
    return <Navigate to="/" replace />;
  }

  if (location.pathname === '/setup') {
    return <><ConfigRecoveryNotice config={config} /><Outlet /></>;
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
    ...(showOrganizationNavigation
      ? [{
          to: '/admin/organization/overview',
          label: t('nav.organization'),
          icon: Building2,
          match: (p: string) => p.startsWith('/admin/organization'),
        }]
      : []),
    // Permanent escape hatch to the App Library (a workbench app) for principals
    // covered by the current Apps policy. It stays reachable even when undocked.
    ...(canUseApps
      ? [{ to: '/apps/library', label: t('nav.appLibrary'), icon: LayoutGrid, match: (p: string) => p.startsWith('/apps/library') }]
      : []),
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
    // 模型 (Model Hub, L4): the backend release capability is the only authority.
    ...(modelHubEnabled
      ? [
          {
            to: '/admin/settings/models',
            label: t('nav.models'),
            icon: Cpu,
            match: (p: string) => p.startsWith('/admin/settings/models'),
          },
        ]
      : []),
    {
      to: '/admin/settings/backends',
      label: t('nav.backends'),
      icon: Bot,
      match: (p) => p.startsWith('/admin/settings/backends'),
    },
    ...(memoryNavVisible
      ? [{ to: '/admin/settings/memory', label: t('memory.betaTitle'), icon: Brain, match: isMemorySettingsPath }]
      : []),
    {
      // 高级设置: the remaining Settings tabs (messaging leads). Platforms,
      // backends, models, and Memory have their own sidebar destinations, so
      // exclude those routes from the active match.
      to: '/admin/settings/messaging',
      label: t('nav.advancedSettings'),
      icon: Settings,
      match: (pathname) => isAdvancedSettingsPath(pathname, memoryNavVisible),
    },
  ];

  // Second half of the runtime-access gate: a page the redirect above withholds
  // must not still be advertised, or a non-owner taps a nav entry and lands
  // back on the Workbench.
  const visibleAdminItems = visibleAdminNavItems(adminItems, capabilities.can_manage_instance);

  const items: ShellNavItem[] = shellMode === 'admin' ? visibleAdminItems : [];

  // A bottom tab bar can't hold the nested admin nav, so mobile keeps a trimmed
  // bar with Workbench, Control Panel, and More (which opens the full nested nav
  // sheet). The Organization tab follows the same capability visibility as
  // every other shell entry point. See ``adminMenuOpen``.
  const adminMobileTabsAll: ShellNavItem[] = [
    { to: '/', label: t('nav.workbench'), icon: Sparkles, variant: 'workbench' },
    { to: '/admin/dashboard', label: t('nav.dashboard'), icon: LayoutDashboard },
    ...(showOrganizationNavigation
      ? [{ to: '/admin/organization/overview', label: t('nav.organization'), icon: Building2 }]
      : []),
    { label: t('nav.more'), icon: Menu, onClick: () => setAdminMenuOpen(true), match: () => adminMenuOpen },
    {
      to: '/admin/settings/messaging',
      label: t('nav.advancedSettings'),
      icon: Settings,
      match: (pathname) => isAdvancedSettingsPath(pathname, memoryNavVisible),
    },
  ];
  const adminMobileTabs = visibleAdminNavItems(adminMobileTabsAll, capabilities.can_manage_instance);
  // The More sheet shows the overflow: admin sections not already on the bottom
  // bar. Keep its filtering aligned with the currently visible primary tabs.
  const adminBottomBarPaths = new Set(
    adminMobileTabs.map((item) => item.to).filter((to): to is string => !!to && to !== '/'),
  );
  const adminSheetItems = visibleAdminItems
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
    ...(capabilities.can_use_agents || capabilities.can_use_skills || capabilities.can_use_vault_secrets ? [{
      to: capabilities.can_use_agents ? '/agents' : capabilities.can_use_skills ? '/skills' : '/vaults',
      label: t('nav.capabilities'),
      icon: LayoutGrid,
      match: (p: string) => ['/agents', '/skills', '/harness', '/vaults'].some((x) => p.startsWith(x)),
    }] : []),
    // Apps (§7.1b): replaces the old 更多 route tab. Tapping toggles the Dock
    // drawer (the mobile Dock) rather than navigating; `grid-2x2` distinguishes
    // it from Capabilities' `layout-grid`.
    ...(canUseApps
      ? [{ label: t('nav.apps'), icon: Grid2x2, onClick: () => setAppsDrawerOpen((v) => !v), match: () => appsDrawerOpen }]
      : []),
  ];

  // Chat is a full-screen detail (own composer) and Search is a full-screen
  // focused surface (own header + back button); the wizard owns the whole
  // viewport. These mobile surfaces render their own top chrome, so the shell's
  // mobile brand header AND the bottom tab bar are hidden on them.
  const isChat = location.pathname.startsWith('/chat/');
  const isSearch = location.pathname === '/search';
  const isFullScreenMobile = isChat || isSearch;

  const showBottomNav = !isFullScreenMobile && !chromeless && location.pathname !== '/setup';

  return (
    // Mobile: a LOCKED, full-viewport flex column (overflow-hidden) so the
    // document never scrolls — iOS can't then fling a focused input off the top —
    // and <main> scrolls internally. The height is the STATIC --app-shell-h (dvh,
    // with a 100vh fallback for older iOS): we deliberately do NOT resize the shell
    // to the visual viewport in JS, because mutating the shell height mid-focus
    // fought iOS's own scroll-into-view and threw the input off-screen. iOS instead
    // pans the locked page to lift the focused composer above the keyboard.
    // Desktop: normal document flow.
    <WindowManagerProvider standalone={standaloneAppTab}>
    <StandaloneAppTabContext.Provider value={standaloneAppTab}>
    <DockProvider enabled={canUseApps}>
    <ShowPageDragProvider>
    {/* Chromeless (single-app tab): the locked full-viewport column applies on DESKTOP too —
        the app fills the browser area exactly, with nothing to scroll around it. */}
    <div
      className={clsx(
        'flex h-[var(--app-shell-h)] flex-col overflow-hidden bg-background text-foreground',
        !chromeless && 'md:block md:h-auto md:min-h-screen md:overflow-visible'
      )}
    >
      <ConfigRecoveryNotice config={config} />
      {/* The sidebar forms its own stacking context BELOW the window layer (aside z-10 < window
          layer z-20), so a maximized window covers the WHOLE sidebar — including the Apps launcher.
          The Apps button no longer floats on top in full-screen (a Dock redesign comes later);
          un-maximize to reach it. */}
      {!chromeless && (
      <aside className="fixed inset-y-0 left-0 z-10 hidden w-[240px] flex-col border-r border-border bg-surface md:flex">
        {/* Workbench packs more rows (search/inbox/capabilities/projects) into the
            sidebar, so it runs a tighter vertical rhythm than admin — less outer
            padding and a smaller gap to the bottom cluster — to give the flex-1
            Projects list more height. Admin keeps the roomier spacing. */}
        <div className="flex h-full flex-col">
          {/* Brand band — flush to the top edge, sharing the chat header's
              px-4 py-2.5 row height so the logo centerline lines up with the
              chat title bar. No bottom border (it read as out of place under
              the logo). Logo is size-8 to match the header's row height. */}
          <Link
            to="/"
            className="group flex shrink-0 items-center gap-2.5 px-4 py-2.5 transition-colors hover:bg-foreground/[0.03] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-mint/60"
          >
            <img
              src={logoImg}
              alt="avibe logo"
              className="size-8 rounded-lg border border-mint/35 bg-mint/[0.08] object-cover shadow-[0_0_16px_-4px_rgba(91,255,160,0.5)] transition-shadow group-hover:shadow-[0_0_20px_-3px_rgba(91,255,160,0.65)]"
            />
            <div className="min-w-0 leading-tight">
              <div className="truncate text-[13px] font-semibold text-foreground">{t('appShell.title')}</div>
              <div className="truncate text-[11px] text-muted">{t('appShell.subtitle')}</div>
            </div>
          </Link>

          {/* Middle: workspace label + nav list (scrolls). Carries the
              horizontal + top padding the outer container used to own; workbench
              runs a tighter rhythm than admin to give the flex-1 Projects list
              more height. */}
          <div className={clsx('flex min-h-0 flex-1 flex-col px-4', shellMode === 'workbench' ? 'gap-3 pt-3' : 'gap-6 pt-4')}>

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
              + hostname between the rows. The whole bottom cluster sits at the
              sidebar's level (z-10) and is covered by a maximized window. The
              outer container no longer owns padding (the brand band is flush to
              the top edge), so this cluster carries its own px-4 + bottom pad. */}
          <div className={clsx('relative flex flex-col gap-3 px-4', shellMode === 'workbench' ? 'pb-4' : 'pb-5')}>
            {/* Row 1 — Apps (Dock trigger, left) paired with the mode switch
                (right). The Dock rises ABOVE the Apps button, clear of the
                centered Chat composer. Workbench → Settings (control panel);
                Control Panel → Back to Workbench, the mint counterpart. */}
            <div className="flex items-stretch gap-2">
              {canUseApps && <AppsLauncher />}
              {shellMode === 'workbench' && capabilities.can_manage_instance && (
                <Link
                  to={adminLandingPath(capabilities.can_use_system)}
                  title={t('appShell.openControlPanel')}
                  aria-label={t('appShell.openControlPanel')}
                  className="group flex w-11 shrink-0 items-center justify-center rounded-lg border border-border-strong text-foreground transition-colors hover:bg-foreground/[0.04]"
                >
                  <Settings className="size-[18px] text-muted group-hover:text-foreground" />
                </Link>
              )}
            </div>

            {/* Back-to-Workbench (admin only) gets its own full-width row below
                Apps. As a half-width button beside Apps, the English "Workbench"
                label + arrow overflowed the 240px sidebar; a full row fits every
                locale. */}
            {shellMode === 'admin' && (
              <Link
                to="/"
                className="flex items-center justify-center gap-2 rounded-lg border border-mint/30 bg-mint/[0.06] px-3 py-2.5 text-[13px] font-semibold text-mint-ink transition hover:bg-mint/[0.12]"
              >
                <ArrowLeft className="size-3.5 shrink-0" />
                <span className="truncate">{t('appShell.backToWorkbench')}</span>
              </Link>
            )}

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
                    isRunning ? 'bg-mint-ink shadow-[0_0_8px_rgba(91,255,160,0.9)]' : 'bg-muted'
                  )}
                />
                {isRunning ? t('common.running') : t('common.stopped')}
              </span>
              {capabilities.can_manage_instance && <VersionBadge openUpward />}
            </div>
          </div>
        </div>
      </aside>
      )}

      {/* Chat and Search are fixed full-screen surfaces with their own header
          bars, so the brand header is hidden there (otherwise it would sit
          behind them). A chromeless single-app tab hides it for the same
          reason: the app owns the viewport. */}
      {!isFullScreenMobile && !chromeless && (
        <header className="sticky top-0 z-40 flex h-[calc(4rem+env(safe-area-inset-top))] shrink-0 items-center justify-between gap-2 border-b border-border bg-background/92 px-4 pt-[env(safe-area-inset-top)] backdrop-blur md:hidden">
          <Link
            to="/"
            className="flex min-w-0 items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/60"
          >
            <img
              src={logoImg}
              alt="avibe logo"
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
      )}

      <main
        className={clsx(
          // Mobile: the internal scroll area of the locked flex-column shell, so
          // the document itself never scrolls. Desktop: normal flow (min-h-screen
          // + sidebar offset).
          chromeless
            // Single-app tab: no sidebar offset, no scroll, no page glow — the app body
            // is the only thing in the viewport and sizes itself to this box (h-full).
            ? 'min-h-0 flex-1 overflow-hidden'
            : 'flex-1 min-h-0 overflow-y-auto md:ml-[240px] md:min-h-screen md:flex-none md:overflow-visible md:pb-0',
          !chromeless && (showBottomNav ? 'pb-[calc(5.5rem+env(safe-area-inset-bottom))]' : 'pb-0'),
          !chromeless &&
            (location.pathname.startsWith('/admin/settings') ? 'page-glow-settings' : 'page-glow-console')
        )}
      >
        <div className={clsx(
          'w-full',
          chromeless
            ? 'h-full'
            : location.pathname.startsWith('/admin/organization')
              ? 'mx-auto'
              : 'mx-auto px-4 py-5 md:px-10 md:py-8',
        )}>
          {/* A crashing page only replaces the content area — the sidebar + chrome stay usable, and
              navigating elsewhere clears the error without a manual retry. Key on location.key (not
              just pathname) so a query-only navigation (e.g. /search?q=…) also resets. */}
          <ErrorBoundary variant="page" resetKeys={[location.key]}>
            <Outlet />
          </ErrorBoundary>
        </div>
      </main>

      {showBottomNav && (
        shellMode === 'admin' ? (
          <MobileTabBar items={adminMobileTabs} />
        ) : (
          <MobileTabBar
            items={workbenchTabs}
            center={capabilities.can_chat
              ? { onClick: () => setNewSessionOpen(true), label: t('appShell.newSession'), icon: Plus }
              : undefined}
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

      {/* Mobile Dock drawer — the workbench Apps tab summons it (§7.1b). Mobile-only
          (md:hidden internally); mounted inside DockProvider so it reads the same
          docked tiles + order as the desktop Dock. */}
      {shellMode === 'workbench' && canUseApps && (
        <MobileDockDrawer open={appsDrawerOpen} onClose={() => setAppsDrawerOpen(false)} />
      )}

      {capabilities.can_chat && (
        <NewSessionSheet
          open={newSessionOpen}
          onClose={() => setNewSessionOpen(false)}
          onOpen={() => setNewSessionOpen(true)}
        />
      )}

      {/* ⌘K message-search palette. Mounted shell-wide so the shortcut works from
          both Workbench and Control Panel; the sidebar field is the workbench
          entry point. */}
      <SearchPalette open={searchOpen} onClose={() => setSearchOpen(false)} />

      {/* App windows float over the workbench main area (desktop). The Dock (P2)
          and the AppsLauncher bridge open windows via the WindowManager. */}
      {/* A maximized window covers the sidebar Apps launcher. We intentionally do NOT float a
          second launcher on top in full-screen anymore (product: avoid the fullscreen floating
          button; a Dock redesign comes later). Un-maximize via the window traffic-lights to reach
          the sidebar launcher. */}
      {canUseApps && <WindowLayer />}
    </div>
    </ShowPageDragProvider>
    </DockProvider>
    </StandaloneAppTabContext.Provider>
    </WindowManagerProvider>
  );
};
