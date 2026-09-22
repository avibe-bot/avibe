import type { TranslationKey } from '@/i18n/types';
import React, { useEffect, useMemo, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  ChevronDown,
  ChevronLeft,
  Cpu,
  Globe,
  Hash,
  Keyboard,
  Layers,
  MessageCircle,
  MessageSquare,
  Package,
  Plug,
  Server,
  Settings,
  ShieldCheck,
  Stethoscope,
  Unplug,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { SETTINGS_LANDING_PATH } from '@/lib/adminNavigation';
import { settingsResumePath, writeLastSettingsSection } from '@/lib/settingsSectionMemory';
import { getEnabledPlatforms, platformSupportsChannels } from '@/lib/platforms';
import { useIsDesktop } from '@/lib/useIsDesktop';
import {
  closeSettingsOverlay,
  isChromelessShellPath,
  useSettingsOverlayContext,
} from '@/lib/settingsOverlay';
import { useStandaloneSettingsMenu } from '@/lib/settingsMenuPlacement';
import { AccountMenu } from '../AccountMenu';
import { VersionBadge } from '../VersionBadge';
import { modelHubEnabledFromConfig } from './models/featureFlags';

type SettingsItem = {
  path: string;
  labelKey: TranslationKey;
  icon: React.ComponentType<{ className?: string }>;
  ownerOnly?: boolean;
  feature?: 'models' | 'channels';
  children?: SettingsItem[];
  defaultOpen?: boolean;
  exact?: boolean;
};

type SettingsGroup = {
  /** Omitted for the landing group, which needs no header above the first row. */
  labelKey?: TranslationKey;
  items: SettingsItem[];
};

const SETTINGS_GROUPS: SettingsGroup[] = [
  {
    items: [
      { path: SETTINGS_LANDING_PATH, labelKey: 'settings.sections.general', icon: Settings },
    ],
  },
  {
    labelKey: 'settings.groups.agents',
    items: [
      { path: '/settings/backends', labelKey: 'settings.sections.backends', icon: Server, ownerOnly: true },
      { path: '/settings/models', labelKey: 'settings.sections.models', icon: Cpu, ownerOnly: true, feature: 'models' },
      { path: '/settings/replies', labelKey: 'settings.sections.replies', icon: MessageSquare },
    ],
  },
  {
    labelKey: 'settings.groups.connections',
    items: [
      {
        path: '/settings/platforms',
        labelKey: 'nav.messagingPlatforms',
        icon: Plug,
        ownerOnly: true,
        defaultOpen: true,
        children: [
          {
            path: '/settings/platforms',
            labelKey: 'settings.sections.platformConnections',
            icon: Unplug,
            exact: true,
          },
          { path: '/settings/platforms/users', labelKey: 'nav.users', icon: MessageCircle },
          {
            path: '/settings/platforms/groups',
            labelKey: 'nav.channels',
            icon: Hash,
            feature: 'channels',
          },
        ],
      },
      { path: '/settings/remote-access', labelKey: 'settings.sections.remoteAccess', icon: Globe, ownerOnly: true },
    ],
  },
  {
    labelKey: 'settings.groups.system',
    items: [
      { path: '/settings/shortcuts', labelKey: 'settings.sections.shortcuts', icon: Keyboard },
      { path: '/settings/service', labelKey: 'settings.sections.service', icon: Layers, ownerOnly: true },
      { path: '/settings/dependencies', labelKey: 'settings.sections.dependencies', icon: Package, ownerOnly: true },
      { path: '/settings/diagnostics', labelKey: 'settings.sections.diagnostics', icon: Stethoscope, ownerOnly: true },
      { path: '/settings/access', labelKey: 'settings.sections.access', icon: ShieldCheck },
    ],
  },
];

const pathMatches = (pathname: string, itemPath: string): boolean =>
  pathname === itemPath || pathname.startsWith(`${itemPath}/`);

const itemMatches = (pathname: string, item: SettingsItem): boolean =>
  item.exact ? pathname === item.path : pathMatches(pathname, item.path);

const normalizedSettingsPath = (path: string): string => path.replace(/\/+$/, '') || '/';

const SettingsNavLink: React.FC<{ item: SettingsItem }> = ({ item }) => {
  const { t } = useTranslation();
  const location = useLocation();
  const active = itemMatches(location.pathname, item);
  const Icon = item.icon;

  return (
    <NavLink
      to={item.path}
      end={item.exact}
      title={t(item.labelKey)}
      className={clsx(
        'flex min-h-11 items-center gap-2 rounded-[9px] px-2.5 py-2 text-[12.5px] transition-colors md:min-h-[34px] md:py-0',
        'md:justify-start',
        active
          ? 'bg-mint-soft font-semibold text-foreground'
          : 'font-medium text-muted hover:bg-foreground/[0.04] hover:text-foreground',
      )}
    >
      <Icon className={clsx('size-3.5 shrink-0', active ? 'text-mint-ink' : 'text-muted')} />
      {/* The rail's width is the layout's to decide and a section name is not
          optional detail: an ellipsis here hides which of two neighbouring pages
          a row leads to ("Messaging Platforms" / "Platform Connections" both cut
          to "Platform…" in English at the inline width). Wrapping keeps every
          label readable in any language and at either width, and two lines at
          this size still fit the row's min height, so nothing moves for the
          labels that already fitted. */}
      <span className="min-w-0 leading-[1.3] break-words">{t(item.labelKey)}</span>
    </NavLink>
  );
};

const SettingsNavGroup: React.FC<{ item: SettingsItem }> = ({ item }) => {
  const { t } = useTranslation();
  const location = useLocation();
  const children = item.children ?? [];
  const childActive = children.some((child) => itemMatches(location.pathname, child));
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const open = manualOpen ?? item.defaultOpen ?? childActive;
  const Icon = item.icon;

  return (
    <div className="flex flex-col gap-0.5">
      <button
        type="button"
        title={t(item.labelKey)}
        aria-expanded={open}
        onClick={() => setManualOpen(!open)}
        className={clsx(
          'flex min-h-11 w-full items-center gap-2 rounded-[9px] px-2.5 py-2 text-[12.5px] font-medium transition-colors md:min-h-[34px] md:py-0',
          'md:justify-start',
          childActive
            ? 'text-foreground'
            : 'text-muted hover:bg-foreground/[0.04] hover:text-foreground',
        )}
      >
        <Icon className={clsx('size-3.5 shrink-0', childActive ? 'text-mint-ink' : 'text-muted')} />
        <span className="min-w-0 flex-1 leading-[1.3] break-words text-left">{t(item.labelKey)}</span>
        <ChevronDown
          className={clsx(
            'size-3.5 shrink-0 text-muted transition-transform',
            open && 'rotate-180',
          )}
        />
      </button>
      {open && (
        <div className="ml-3 flex flex-col gap-0.5 border-l border-border pl-2">
          {children.map((child) => <SettingsNavLink key={child.path} item={child} />)}
        </div>
      )}
    </div>
  );
};

const SettingsNavItem: React.FC<{ item: SettingsItem }> = ({ item }) =>
  item.children?.length ? <SettingsNavGroup item={item} /> : <SettingsNavLink item={item} />;

/**
 * Leaving Settings has one meaning regardless of the control that triggers it:
 * an overlay returns to the surface it covered, a direct visit goes to the
 * Workbench. Both paths keep the originating project, session and unsent draft.
 */
const ReturnToApp: React.FC<{
  className?: string;
  'aria-label'?: string;
  children: React.ReactNode;
}> = ({ className, 'aria-label': ariaLabel, children }) => {
  const navigate = useNavigate();
  const overlayOrigin = useSettingsOverlayContext();

  if (!overlayOrigin) {
    return (
      <NavLink to="/" aria-label={ariaLabel} className={className}>
        {children}
      </NavLink>
    );
  }
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      onClick={() => closeSettingsOverlay(navigate, overlayOrigin)}
      className={className}
    >
      {children}
    </button>
  );
};

export const SettingsLayout: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { capabilities } = useInstanceAuthorization();
  const location = useLocation();
  const navigate = useNavigate();
  const isDesktop = useIsDesktop();
  // Specifically the setup wizard, and only for the mobile back affordance
  // below: coming from the wizard makes Back mean Back. That is a different
  // question from whether a sidebar is on screen, which the rail reads from the
  // shell instead.
  const setupOriginPath = useSettingsOverlayContext()?.location.pathname;
  const setupOrigin = setupOriginPath !== undefined && isChromelessShellPath(setupOriginPath);
  const standaloneMenu = useStandaloneSettingsMenu();
  const [modelHubVisible, setModelHubVisible] = useState(false);
  const [channelSettingsVisible, setChannelSettingsVisible] = useState(false);
  const atRoot = location.pathname === '/settings' || location.pathname === '/settings/';
  const isModelHub = pathMatches(location.pathname, '/settings/models');
  // Settings is a standalone page: ordinary sections use one 944px outer
  // frame with an 880px content column after 32px desktop padding. Model Hub
  // keeps its full route-pane width and its own full-height surface.
  const isFluidContent = isModelHub;

  useEffect(() => {
    if (!capabilities.can_manage_instance) return;
    let cancelled = false;
    let configVersion = 0;
    const applyConfigVisibility = (config: unknown) => {
      setModelHubVisible(modelHubEnabledFromConfig(config));
      setChannelSettingsVisible(
        getEnabledPlatforms(config).some((platform) => platformSupportsChannels(config, platform)),
      );
    };
    const stopConfigChanges = api.onConfigChanged((config) => {
      if (cancelled) return;
      configVersion += 1;
      applyConfigVisibility(config);
    });
    const requestedConfigVersion = configVersion;
    void api.getConfig()
      .then((config) => {
        if (!cancelled && requestedConfigVersion === configVersion) applyConfigVisibility(config);
      })
      .catch(() => {
        if (!cancelled && requestedConfigVersion === configVersion) {
          setModelHubVisible(false);
          setChannelSettingsVisible(false);
        }
      });
    return () => {
      cancelled = true;
      stopConfigChanges();
    };
  }, [api, capabilities.can_manage_instance]);

  const visibleGroups = useMemo(
    () => SETTINGS_GROUPS.map((group) => ({
      ...group,
      items: group.items.flatMap((item) => {
        if (item.ownerOnly && !capabilities.can_manage_instance) return [];
        if (item.feature === 'models' && !modelHubVisible) return [];
        return [{
          ...item,
          children: item.children?.filter((child) =>
            child.feature !== 'channels' || channelSettingsVisible),
        }];
      }),
    })).filter((group) => group.items.length > 0),
    [capabilities.can_manage_instance, channelSettingsVisible, modelHubVisible],
  );

  const activeTrail = useMemo(() => {
    // Route hierarchy must stay stable while capability/config projections load;
    // otherwise a mobile deep link can briefly point its Back action at the
    // wrong parent before its rail item becomes visible.
    for (const group of SETTINGS_GROUPS) {
      for (const item of group.items) {
        const child = item.children?.find((candidate) => itemMatches(location.pathname, candidate));
        if (child) return [item, child];
        if (pathMatches(location.pathname, item.path)) return [item];
      }
    }
    return [];
  }, [location.pathname]);

  const mobileBackTarget = useMemo(() => {
    if (atRoot || setupOrigin) return '/';
    const activeSection = activeTrail.at(-1);
    if (!activeSection) return '/settings';
    return normalizedSettingsPath(location.pathname) === normalizedSettingsPath(activeSection.path)
      ? '/settings'
      : activeSection.path;
  }, [activeTrail, atRoot, location.pathname, setupOrigin]);

  // One class string for both branches below, so the touch target and the
  // chevron cannot drift apart depending on where the control points.
  const mobileBackClassName = '-ml-2 grid size-11 shrink-0 place-items-center rounded-lg text-muted transition hover:bg-foreground/[0.05] hover:text-foreground md:hidden';

  const mobileBackLabel = setupOrigin ? t('common.back') : mobileBackTarget === '/'
    ? t('settings.backToWorkbench')
    : mobileBackTarget === '/settings'
      ? t('settings.backToSections')
      : t('settings.backToSection', {
        section: t(activeTrail.at(-1)?.labelKey ?? 'nav.settings'),
      });

  // Choosing a rail row is a lasting preference, not a step inside one visit,
  // so the next ordinary entry resumes it. The trail's last item is what gets
  // recorded: a detail page inside a section resumes at the section that owns
  // it, which is the row the rail can show as current.
  //
  // Where the person actually is, is the whole input. A feature-gated row
  // (Models, Channels) can leave the rail while its page stays routed.
  // Nothing here consults the feature projections the rail draws
  // rows from, so a pending or failed read can neither erase a preference nor
  // record the wrong one.
  useEffect(() => {
    const section = activeTrail.at(-1);
    if (section) writeLastSettingsSection(section.path);
  }, [activeTrail]);

  // The root is the phone's section list — the one screen a viewport with no
  // rail beside the page can navigate from, and what its entry points at. A
  // desktop keeps that rail on screen, so the root has nothing left to show
  // there and resolves through to a section: the resumed one, the same answer
  // its entry link resolves for itself.
  useEffect(() => {
    if (!atRoot || !isDesktop) return;
    navigate(settingsResumePath(capabilities.can_manage_instance), { replace: true });
  }, [atRoot, capabilities.can_manage_instance, isDesktop, navigate]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background md:h-[var(--app-shell-h)]">
      <header className="flex h-[calc(3.5rem+env(safe-area-inset-top))] shrink-0 items-center justify-between border-b border-border bg-surface px-4 pt-[env(safe-area-inset-top)] md:h-14 md:pt-0">
        <div className="flex min-w-0 items-center gap-2 text-[13px] font-semibold text-foreground">
          {/* Leaving Settings for the Workbench is the same action whichever
              control triggers it, so the phone's root back goes through
              ReturnToApp like the desktop close and rail row do: with a retained
              origin it unwinds the Settings entries it opened over, instead of
              pushing a second home in front of them and leaving the whole chain
              one Back away. Every destination inside Settings stays an ordinary
              link, and a direct visit still gets one. */}
          {mobileBackTarget === '/' ? (
            <ReturnToApp aria-label={mobileBackLabel} className={mobileBackClassName}>
              <ChevronLeft className="size-5" />
            </ReturnToApp>
          ) : (
            <NavLink
              to={mobileBackTarget}
              aria-label={mobileBackLabel}
              className={mobileBackClassName}
            >
              <ChevronLeft className="size-5" />
            </NavLink>
          )}
          <Settings className={clsx('size-4 text-mint-ink', mobileBackTarget && 'hidden md:block')} />
          <span>{t('nav.settings')}</span>
          {!atRoot && (
            <>
              <span className="text-border-strong">/</span>
              <span className="truncate font-normal text-muted">
                {activeTrail.map((item) => t(item.labelKey)).join(' / ') || t('nav.settings')}
              </span>
            </>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {capabilities.can_manage_instance && (
            <div className="md:hidden">
              <VersionBadge />
            </div>
          )}
          <ReturnToApp
            aria-label={t('settings.close')}
            className="hidden size-8 shrink-0 place-items-center rounded-lg text-muted transition hover:bg-foreground/[0.05] hover:text-foreground md:grid"
          >
            <X className="size-4" />
          </ReturnToApp>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* Standalone Settings replaces the app sidebar, so this rail has to be
            the same width the sidebar was — including a width the owner dragged
            it to — or the left column jumps the moment Settings opens. Inline
            Settings sits BESIDE that sidebar, where matching it would spend a
            second full-width column on a secondary nav, so it keeps 196. */}
        <nav
          aria-label={t('settings.navigationLabel')}
          className={clsx(
            'min-h-0 shrink-0 border-r border-border bg-surface/70 px-2 pb-[calc(0.75rem+env(safe-area-inset-bottom))] pt-3 md:pb-3',
            'w-full flex-col',
            standaloneMenu ? 'md:w-[var(--app-sidebar-w)]' : 'md:w-[196px]',
            atRoot ? 'flex' : 'hidden md:flex',
          )}
        >
          <ReturnToApp
            aria-label={t('settings.backToApp')}
            className="mb-2 hidden min-h-10 shrink-0 items-center gap-2.5 rounded-[9px] px-2.5 text-[14px] text-foreground transition-colors hover:bg-foreground/[0.05] md:flex md:justify-start"
          >
            <ArrowLeft className="size-[17px] shrink-0" />
            <span className="truncate">{t('settings.backToApp')}</span>
          </ReturnToApp>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {visibleGroups.map((group) => (
              <div key={group.labelKey ?? group.items[0]?.path} className="mb-2 last:mb-0">
                {group.labelKey && (
                  <div className="px-2 pb-1 pt-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted">
                    {t(group.labelKey)}
                  </div>
                )}
                <div className="flex flex-col gap-0.5">
                  {/* A manual disclosure choice belongs to this route visit only. */}
                  {group.items.map((item) => (
                    <SettingsNavItem
                      key={item.children?.length ? `${item.path}:${location.pathname}` : item.path}
                      item={item}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="mt-3 flex shrink-0 items-center gap-2 border-t border-border px-2 pt-3">
            <AccountMenu openUpward />
          </div>
        </nav>

        <section className={clsx('min-w-0 flex-1 overflow-y-auto', atRoot && 'hidden md:block')}>
          <div
            key={location.pathname}
            className={clsx(
              'w-full px-4 pb-[calc(1.25rem+env(safe-area-inset-bottom))] pt-5 motion-safe:animate-in motion-safe:slide-in-from-right-4 motion-safe:duration-200 md:px-8 md:pb-7 md:pt-7 md:animate-none',
              isModelHub && 'min-h-full',
              !isFluidContent && 'mx-auto max-w-[944px]',
            )}
          >
            <Outlet />
          </div>
        </section>
      </div>
    </div>
  );
};
