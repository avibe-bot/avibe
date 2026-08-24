import React from 'react';
import { useTranslation } from 'react-i18next';

import { LogsPanel } from '@/components/steps/LogsPanel';
import { SettingsPageShell } from './SettingsPageShell';
import { DiagnosticsSectionTabs } from './SettingsDiagnosticsPage';

export const SettingsLogsPage: React.FC = () => {
  const { t } = useTranslation();

  return (
    <SettingsPageShell
      activeTab="diagnostics"
      title={t('settings.diagnosticsTitle')}
      subtitle={t('settings.diagnosticsSubtitle')}
    >
      <DiagnosticsSectionTabs />
      <LogsPanel titleKey="settings.logsTitle" />
    </SettingsPageShell>
  );
};
