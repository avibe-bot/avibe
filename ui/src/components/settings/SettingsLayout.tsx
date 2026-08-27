import React, { useEffect, useMemo, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  Bot,
  Brain,
  ChevronDown,
  ChevronLeft,
  Cpu,
  Globe,
  Hash,
  Keyboard,
  MessageCircle,
  MessageSquare,
  Package,
  PlugZap,
  Server,
  Settings,
  ShieldCheck,
  Stethoscope,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { memoryNavShouldBeVisible } from '@/lib/memorySettings';
import { rememberSettingsPath, settingsLandingPath } from '@/lib/adminNavigation';
import { getEnabledPlatforms, platformSupportsChannels } from '@/lib/platforms';
import { useIsDesktop } from '@/lib/useIsDesktop';
import { AccountMenu } from '../AccountMenu';
import { LanguageSwitcher } from '../LanguageSwitcher';
import { ThemeToggle } from '../ThemeToggle';
import { modelHubEnabledFromConfig } from './models/featureFlags';

type SettingsItem = {
  path: string;
  labelKey: string;
  icon: React.ComponentType<{ className?: string }>;
  ownerOnly?: boolean;
  feature?: 'models' | 'memory' | 'channels';
  children?: SettingsItem[];
  defaultOpen?: boolean;
  exact?: boolean;
};

type SettingsGroup = {
  labelKey: string;
  items: SettingsItem[];
};

const SETTINGS_GROUPS: SettingsGroup[] = [
  {
    labelKey: 'settings.groups.agents',
    items: [
      { path: '/settings/backends', labelKey: 'settings.sections.backends', icon: Bot, ownerOnly: true },
      { path: '/settings/models', labelKey: 'settings.sections.models', icon: Cpu, ownerOnly: true, feature: 'models' },
      { path: '/settings/memory', labelKey: 'settings.sections.memory', icon: Brain, ownerOnly: true, feature: 'memory' },
      { path: '/settings/replies', labelKey: 'settings.sections.replies', icon: MessageSquare },
    ],
  },
  {
    labelKey: 'settings.groups.connections',
    items: [
      {
        path: '/settings/platforms',
        labelKey: 'nav.messagingPlatforms',
        icon: PlugZap,
        ownerOnly: true,
        defaultOpen: true,
        children: [
          {
            path: '/settings/platforms',
            labelKey: 'settings.sections.platformConnections',
            icon: PlugZap,
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
      { path: '/settings/service', labelKey: 'settings.sections.service', icon: Server, ownerOnly: true },
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
        'flex min-h-11 items-center gap-2 rounded-lg px-2 py-2 text-[12.5px] font-medium transition-colors md:min-h-0',
        'md:justify-center lg:justify-start',
        active
          ? 'bg-mint/[0.09] text-foreground'
          : 'text-muted hover:bg-foreground/[0.04] hover:text-foreground',
      )}
    >
      <Icon className={clsx('size-4 shrink-0', active ? 'text-mint-ink' : 'text-muted')} />
      <span className="truncate md:hidden lg:block">{t(item.labelKey)}</span>
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
          'flex min-h-11 w-full items-center gap-2 rounded-lg px-2 py-2 text-[12.5px] font-medium transition-colors md:min-h-0',
          'md:justify-center lg:justify-start',
          childActive
            ? 'text-foreground'
            : 'text-muted hover:bg-foreground/[0.04] hover:text-foreground',
        )}
      >
        <Icon className={clsx('size-4 shrink-0', childActive ? 'text-mint-ink' : 'text-muted')} />
        <span className="min-w-0 flex-1 truncate text-left md:hidden lg:block">{t(item.labelKey)}</span>
        <ChevronDown
          className={clsx(
            'size-3.5 shrink-0 text-muted transition-transform md:hidden lg:block',
            open && 'rotate-180',
          )}
        />
      </button>
      {open && (
        <div className="ml-3 flex flex-col gap-0.5 border-l border-border pl-2 md:ml-0 md:border-l-0 md:pl-0 lg:ml-3 lg:border-l lg:pl-2">
          {children.map((child) => <SettingsNavLink key={child.path} item={child} />)}
        </div>
      )}
    </div>
  );
};

const SettingsNavItem: React.FC<{ item: SettingsItem }> = ({ item }) =>
  item.children?.length ? <SettingsNavGroup item={item} /> : <SettingsNavLink item={item} />;

export const SettingsLayout: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { capabilities } = useInstanceAuthorization();
  const location = useLocation();
  const navigate = useNavigate();
  const isDesktop = useIsDesktop();
  const [modelHubVisible, setModelHubVisible] = useState(false);
  const [memoryVisible, setMemoryVisible] = useState(false);
  const [channelSettingsVisible, setChannelSettingsVisible] = useState(false);
  const atRoot = location.pathname === '/settings' || location.pathname === '/settings/';
  const isModelHub = pathMatches(location.pathname, '/settings/models');

  useEffect(() => {
    if (!capabilities.can_manage_instance) return;
    let cancelled = false;
    let memoryRequest = 0;
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
    const refreshMemoryVisibility = () => {
      const request = ++memoryRequest;
      void api.getMemorySettings()
        .then((memory) => {
          if (!cancelled && request === memoryRequest) {
            setMemoryVisible(memoryNavShouldBeVisible(memory));
          }
        })
        .catch(() => {
          if (!cancelled && request === memoryRequest) setMemoryVisible(false);
        });
    };
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
    refreshMemoryVisibility();
    window.addEventListener('avibe:memory-settings-changed', refreshMemoryVisibility);
    return () => {
      cancelled = true;
      stopConfigChanges();
      window.removeEventListener('avibe:memory-settings-changed', refreshMemoryVisibility);
    };
  }, [api, capabilities.can_manage_instance]);

  const visibleGroups = useMemo(
    () => SETTINGS_GROUPS.map((group) => ({
      ...group,
      items: group.items.flatMap((item) => {
        if (item.ownerOnly && !capabilities.can_manage_instance) return [];
        if (item.feature === 'models' && !modelHubVisible) return [];
        if (item.feature === 'memory' && !memoryVisible) return [];
        return [{
          ...item,
          children: item.children?.filter((child) =>
            child.feature !== 'channels' || channelSettingsVisible),
        }];
      }),
    })).filter((group) => group.items.length > 0),
    [capabilities.can_manage_instance, channelSettingsVisible, memoryVisible, modelHubVisible],
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
    if (atRoot) return null;
    const activeSection = activeTrail.at(-1);
    if (!activeSection) return '/settings';
    return normalizedSettingsPath(location.pathname) === normalizedSettingsPath(activeSection.path)
      ? '/settings'
      : activeSection.path;
  }, [activeTrail, atRoot, location.pathname]);

  const mobileBackLabel = mobileBackTarget === '/settings'
    ? t('settings.backToSections')
    : t('settings.backToSection', {
      section: t(activeTrail.at(-1)?.labelKey ?? 'nav.settings'),
    });

  useEffect(() => {
    if (atRoot || !location.pathname.startsWith('/settings/')) return;
    rememberSettingsPath(location.pathname);
  }, [atRoot, location.pathname]);

  useEffect(() => {
    if (!atRoot || !isDesktop) return;
    navigate(settingsLandingPath(capabilities.can_manage_instance), { replace: true });
  }, [atRoot, capabilities.can_manage_instance, isDesktop, navigate]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background md:h-[var(--app-shell-h)]">
      <header className="flex h-[calc(3.5rem+env(safe-area-inset-top))] shrink-0 items-center justify-between border-b border-border bg-surface px-4 pt-[env(safe-area-inset-top)] md:h-14 md:pt-0">
        <div className="flex min-w-0 items-center gap-2 text-[13px] font-semibold text-foreground">
          {mobileBackTarget && (
            <NavLink
              to={mobileBackTarget}
              aria-label={mobileBackLabel}
              className="-ml-2 grid size-10 shrink-0 place-items-center rounded-lg text-muted transition hover:bg-foreground/[0.05] hover:text-foreground md:hidden"
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
        <NavLink
          to="/"
          aria-label={t('settings.close')}
          className="grid size-10 shrink-0 place-items-center rounded-lg text-muted transition hover:bg-foreground/[0.05] hover:text-foreground md:size-8"
        >
          <X className="size-4" />
        </NavLink>
      </header>

      <div className="flex min-h-0 flex-1">
        <nav
          aria-label={t('settings.navigationLabel')}
          className={clsx(
            'min-h-0 shrink-0 border-r border-border bg-surface/70 px-2 pb-[calc(0.75rem+env(safe-area-inset-bottom))] pt-3 md:pb-3',
            'w-full flex-col md:w-14 lg:w-[196px]',
            atRoot ? 'flex' : 'hidden md:flex',
          )}
        >
          <div className="min-h-0 flex-1 overflow-y-auto">
            {visibleGroups.map((group) => (
              <div key={group.labelKey} className="mb-2 last:mb-0">
                <div className="px-2 pb-1 pt-1 font-mono text-[9px] font-bold uppercase tracking-[0.16em] text-muted md:hidden lg:block">
                  {t(group.labelKey)}
                </div>
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

          <div className="mt-3 flex shrink-0 items-center gap-2 border-t border-border px-2 pt-3 md:flex-col lg:flex-row">
            <LanguageSwitcher openUpward />
            <ThemeToggle />
            <AccountMenu openUpward />
          </div>
        </nav>

        <section className={clsx('min-w-0 flex-1 overflow-y-auto', atRoot && 'hidden md:block')}>
          <div
            key={location.pathname}
            className={clsx(
              'w-full px-4 pb-[calc(1.25rem+env(safe-area-inset-bottom))] pt-5 motion-safe:animate-in motion-safe:slide-in-from-right-4 motion-safe:duration-200 md:px-6 md:pb-7 md:pt-7 md:animate-none lg:px-8',
              isModelHub ? 'min-h-full' : 'mx-auto max-w-[1180px]',
            )}
          >
            <Outlet />
          </div>
        </section>
      </div>
    </div>
  );
};
