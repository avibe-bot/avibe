import React from 'react';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import {
  MessageSquare,
  Package,
  Server,
  Stethoscope,
} from 'lucide-react';

type SettingsTab = 'service' | 'platforms' | 'backends' | 'dependencies' | 'messaging' | 'diagnostics';

const TABS: Array<{
  key: SettingsTab;
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}> = [
  // Platforms + Backends were promoted to their own sidebar destinations (平台
  // under 通讯平台, and a top-level 后端), so they're no longer Advanced-Settings
  // tabs. Messaging leads, per the requested order.
  { key: 'messaging', href: '/settings/messaging', label: 'settings.tabs.messaging', icon: MessageSquare },
  { key: 'service', href: '/settings/service', label: 'settings.tabs.service', icon: Server },
  { key: 'dependencies', href: '/settings/dependencies', label: 'settings.tabs.dependencies', icon: Package },
  { key: 'diagnostics', href: '/settings/diagnostics', label: 'settings.tabs.diagnostics', icon: Stethoscope },
];

export type SettingsPageShellProps = {
  title: string;
  subtitle: string;
  activeTab: SettingsTab;
  breadcrumb?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
};

// Mirrors design.pen l6PdZd → wH3uC (sTabs):
// underline tabs over a 1px --border baseline. Each tab is padding [10, 16],
// 13px Inter, gap 8 between icon + label. Inactive: muted text/icon, font 500.
// Active: mint 2px bottom border, foreground text, mint icon, font 600.
export const SettingsPageShell: React.FC<SettingsPageShellProps> = ({
  title,
  subtitle,
  activeTab,
  breadcrumb,
  actions,
  children,
}) => {
  const { t } = useTranslation();
  // Platforms + Backends now live in the sidebar, not as Advanced-Settings tabs,
  // so when one of those standalone pages renders this shell we hide the tab bar
  // (its activeTab isn't one of the remaining tabs).
  const showTabs = TABS.some((tab) => tab.key === activeTab);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1.5">
          <h1 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">{title}</h1>
          <p className="max-w-3xl text-[14px] leading-[1.55] text-muted">{subtitle}</p>
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>

      {showTabs && (
        <div className="border-b border-border">
          <nav className="-mb-px flex gap-1 overflow-x-auto pb-px" aria-label="Settings sections">
            {TABS.map((tab) => {
              const Icon = tab.icon;
              const active = tab.key === activeTab;
              return (
                <Link
                  key={tab.key}
                  to={tab.href}
                  className={clsx(
                    'inline-flex shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-4 py-2.5 text-[13px] transition-colors',
                    active
                      ? 'border-mint font-semibold text-foreground'
                      : 'border-transparent font-medium text-muted hover:border-border-strong hover:text-foreground'
                  )}
                >
                  <Icon className={clsx('size-3.5', active ? 'text-mint' : 'text-muted')} />
                  {t(tab.label)}
                </Link>
              );
            })}
          </nav>
        </div>
      )}

      {/* Breadcrumb lives below the tabs so it reads as sub-navigation
          *within* the active tab (e.g. "← Back to backends" while on the
          Backends tab). Placing it above the title/tabs would imply it
          navigates out of Settings entirely — a hierarchy mismatch
          flagged in page feedback for /settings/backends/{claude,codex}. */}
      {breadcrumb && <div className="font-mono text-[11px] text-muted">{breadcrumb}</div>}

      <div className="flex flex-col gap-4">{children}</div>
    </div>
  );
};
