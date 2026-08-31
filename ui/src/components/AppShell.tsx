import React, { useEffect, useState } from 'react';
import { Link, Navigate, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { AlertTriangle, FolderTree, Grid2x2, Inbox, LayoutGrid, Plus, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { APP_TAB_PARAM, isStandaloneAppRoutePath, isStandaloneAppTab } from '../apps/appLaunch';
import { StandaloneAppTabContext } from '../context/StandaloneAppTabContext';
import { useApi } from '../context/ApiContext';
import { useStatus } from '../context/StatusContext';
import { useWorkbenchInbox } from '../context/WorkbenchInboxContext';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
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
import { useViewportHeightVar } from '../lib/useViewportHeightVar';
import { APP_SHELL_SCROLL_ID, forgetMobileProjectsListUnlessPreserved } from '../lib/mobileProjectsListMemory';
import { useIsDesktop } from '../lib/useIsDesktop';
import {
  isOwnerOnlyPath,
  settingsLandingPath,
} from '../lib/adminNavigation';
import {
  closeSettingsOverlay,
  isSettingsEntryPath,
  isSettingsRoutePath,
  useSettingsOverlayOrigin,
} from '../lib/settingsOverlay';
import { SettingsOverlayNavigationBoundary } from './settings/SettingsOverlayNavigationBoundary';

type ShellNavItem = {
  to?: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  match?: (pathname: string) => boolean;
  badge?: number;
  onClick?: () => void;
};

const MobileNavLink: React.FC<{ item: ShellNavItem }> = ({ item }) => {
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

// Workbench mobile tabs flank a raised new-session action.
const MobileTabBar: React.FC<{ items: ShellNavItem[]; center?: CenterButton }> = ({ items, center }) => {
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
        to="/settings/diagnostics"
        className="shrink-0 text-[12px] font-semibold text-gold-ink hover:underline"
      >
        {t('configRecovery.action')}
      </Link>
    </div>
  );
};

export const AppShell: React.FC = () => {
  const { t, i18n } = useTranslation();
  const { status } = useStatus();
  // Badge only — the shell is mounted on every route and renders no feed.
  const { totalUnread } = useWorkbenchInbox({ feed: false });
  // The sidebar's own <aside> is hidden below md by CSS, which hides it without
  // unmounting it — so its inbox-feed and project-tree consumers would still
  // fetch on a phone. A demand gate keyed on mounting is only honest if mounting
  // implies visible, so the mount site has to carry the viewport too.
  const isDesktop = useIsDesktop();
  const {
    capabilities,
    remote,
  } = useInstanceAuthorization();
  const api = useApi();
  const location = useLocation();
  const navigate = useNavigate();
  const settingsOverlayOrigin = useSettingsOverlayOrigin(location);
  const settingsOpen = isSettingsRoutePath(location.pathname);
  const settingsOverlayOpen = isDesktop
    && isSettingsEntryPath(location.pathname)
    && settingsOverlayOrigin !== null;
  const surfaceLocation = settingsOverlayOpen ? settingsOverlayOrigin.location : location;
  useEffect(() => {
    forgetMobileProjectsListUnlessPreserved(location.pathname);
  }, [location.pathname]);
  const [config, setConfig] = useState<any>(null);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
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
      if (c.language && c.language !== i18n.language) {
        void i18n.changeLanguage(c.language);
      }
    }).catch(() => {});
  }, [api, capabilities.can_manage_instance, i18n]);

  // Global ⌘K / Ctrl+K toggles the message-search palette. A closer surface may
  // consume the same user-configured chord first; otherwise search owns it.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setSearchOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  // Close the mobile Dock drawer on navigation.
  useEffect(() => {
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
  const chromeless = standaloneAppTab && isStandaloneAppRoutePath(surfaceLocation.pathname);

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
              <Link to="/settings/service">
                {t('setup.remoteOwner.action')}
              </Link>
            </Button>
          </CardContent>
        </Card>
      </main>
    );
  }

  const isRunning = status.state === 'running';
  const canUseApps = capabilities.can_chat;
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

  // Chat is a full-screen detail (own composer), Search is a full-screen focused
  // surface (own header + back button), and built-in apps own their toolbars.
  // These mobile surfaces render their own top chrome, so the shell's mobile
  // brand header AND the bottom tab bar are hidden on them.
  const isChat = surfaceLocation.pathname.startsWith('/chat/');
  const isSearch = surfaceLocation.pathname === '/search';
  const isSettings = isSettingsRoutePath(surfaceLocation.pathname);
  const isShowPageApp = surfaceLocation.pathname.startsWith('/apps/show/');
  const isBuiltinApp = isStandaloneAppRoutePath(surfaceLocation.pathname);
  const isFullScreenMobile = isChat || isSearch || isSettings || isShowPageApp || isBuiltinApp;

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
    <SettingsOverlayNavigationBoundary desktop={isDesktop}>
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
              className="size-8 rounded-lg border border-mint/35 bg-mint/[0.08] object-cover shadow-glow-sm-mint transition-shadow group-hover:shadow-glow-md-mint"
            />
            <div className="min-w-0 leading-tight">
              <div className="truncate text-[13px] font-semibold text-foreground">{t('appShell.title')}</div>
              <div className="truncate text-[11px] text-muted">{t('appShell.subtitle')}</div>
            </div>
          </Link>

          <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 pt-3">
            {isDesktop && <WorkbenchSidebar onOpenSearch={() => setSearchOpen(true)} />}
          </div>

          {/* Bottom (design.pen NbPMq): Apps + Settings, then run state + version.
              Preferences now live with the rest of Settings. The whole bottom cluster sits at the
              sidebar's level (z-10) and is covered by a maximized window. The
              outer container no longer owns padding (the brand band is flush to
              the top edge), so this cluster carries its own px-4 + bottom pad. */}
          <div className="relative flex flex-col gap-3 px-4 pb-4">
            <div className="flex items-stretch gap-2">
              {canUseApps && <AppsLauncher />}
              {settingsOpen ? (
                <button
                  type="button"
                  onClick={() => {
                    if (settingsOverlayOrigin) closeSettingsOverlay(navigate, settingsOverlayOrigin);
                    else navigate('/');
                  }}
                  title={t('appShell.openControlPanel')}
                  aria-label={t('appShell.openControlPanel')}
                  className="group flex w-11 shrink-0 items-center justify-center rounded-lg border border-mint/40 bg-mint/[0.08] text-foreground transition-colors"
                >
                  <Settings className="size-[18px] text-mint-ink" />
                </button>
              ) : (
                <Link
                  to={isDesktop
                    ? settingsLandingPath(capabilities.can_manage_instance)
                    : '/settings'}
                  title={t('appShell.openControlPanel')}
                  aria-label={t('appShell.openControlPanel')}
                  className="group flex w-11 shrink-0 items-center justify-center rounded-lg border border-border-strong text-foreground transition-colors hover:bg-foreground/[0.04]"
                >
                  <Settings className="size-[18px] text-muted group-hover:text-foreground" />
                </Link>
              )}
            </div>

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
                    isRunning ? 'bg-mint shadow-glow-dot-mint' : 'bg-muted'
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
        id={APP_SHELL_SCROLL_ID}
        className={clsx(
          // Mobile: the internal scroll area of the locked flex-column shell, so
          // the document itself never scrolls. Desktop: normal flow (min-h-screen
          // + sidebar offset).
          chromeless
            // Single-app tab: no sidebar offset, no scroll, no page glow — the app body
            // is the only thing in the viewport and sizes itself to this box (h-full).
            ? 'min-h-0 flex-1 overflow-hidden'
            : isFullScreenMobile
              ? 'min-h-0 flex-1 overflow-hidden md:ml-[240px] md:min-h-screen md:flex-none md:overflow-visible md:pb-0'
            : 'flex-1 min-h-0 overflow-y-auto md:ml-[240px] md:min-h-screen md:flex-none md:overflow-visible md:pb-0',
          !chromeless && (showBottomNav ? 'pb-[calc(5.5rem+env(safe-area-inset-bottom))]' : 'pb-0'),
          !chromeless && (isSettings ? 'page-glow-settings' : 'page-glow-console')
        )}
      >
        <div className={clsx(
          'w-full',
          chromeless
            ? 'h-full'
            : isSettings
              ? 'h-full p-0 md:h-auto md:min-h-screen'
              : isFullScreenMobile
                ? 'h-full p-0 md:mx-auto md:h-auto md:px-10 md:py-8'
            : 'mx-auto px-4 py-5 md:px-10 md:py-8',
        )}>
          {/* A crashing page only replaces the content area — the sidebar + chrome stay usable, and
              navigating elsewhere clears the error without a manual retry. Key on location.key (not
              just pathname) so a query-only navigation (e.g. /search?q=…) also resets. */}
          <ErrorBoundary variant="page" resetKeys={[surfaceLocation.key]}>
            <Outlet />
          </ErrorBoundary>
        </div>
      </main>

      {showBottomNav && (
        <MobileTabBar
          items={workbenchTabs}
          center={capabilities.can_chat
            ? { onClick: () => setNewSessionOpen(true), label: t('appShell.newSession'), icon: Plus }
            : undefined}
        />
      )}

      {/* Mobile Dock drawer — the workbench Apps tab summons it (§7.1b). Mobile-only
          (md:hidden internally); mounted inside DockProvider so it reads the same
          docked tiles + order as the desktop Dock. */}
      {canUseApps && (
        <MobileDockDrawer open={appsDrawerOpen} onClose={() => setAppsDrawerOpen(false)} />
      )}

      {capabilities.can_chat && (
        <NewSessionSheet
          open={newSessionOpen}
          onClose={() => setNewSessionOpen(false)}
          onOpen={() => setNewSessionOpen(true)}
        />
      )}

      {/* ⌘K message-search palette. Mounted shell-wide; the sidebar field is the
          Workbench entry point. */}
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
    </SettingsOverlayNavigationBoundary>
  );
};
