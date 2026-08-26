import React from 'react';
import { NavLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { FileText, Stethoscope } from 'lucide-react';
import clsx from 'clsx';

import { DoctorPanel } from '@/components/steps/DoctorPanel';
import { SettingsPageShell } from './SettingsPageShell';

export const DiagnosticsSectionTabs: React.FC = () => {
  const { t } = useTranslation();
  const tabs = [
    { to: '/settings/diagnostics', label: t('settings.diagnosticsTab'), icon: Stethoscope, end: true },
    { to: '/settings/diagnostics/logs', label: t('settings.logsTab'), icon: FileText, end: false },
  ];

  return (
    <nav
      aria-label={t('settings.diagnosticsNavigationLabel')}
      className="inline-flex w-fit rounded-lg border border-border bg-foreground/[0.03] p-1"
    >
      {tabs.map((tab) => {
        const Icon = tab.icon;
        return (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.end}
            className={({ isActive }) => clsx(
              'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors',
              isActive ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-foreground',
            )}
          >
            <Icon className="size-3.5" />
            {tab.label}
          </NavLink>
        );
      })}
    </nav>
  );
};

export const SettingsDiagnosticsPage: React.FC = () => {
  const { t } = useTranslation();

  return (
    <SettingsPageShell
      activeTab="diagnostics"
      title={t('settings.diagnosticsTitle')}
      subtitle={t('settings.diagnosticsSubtitle')}
    >
      <DiagnosticsSectionTabs />
      <DoctorPanel isPage logsPath="/settings/diagnostics/logs" titleKey="settings.diagnosticsDoctorTitle" />
    </SettingsPageShell>
  );
};
