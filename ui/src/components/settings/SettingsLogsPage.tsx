import React from 'react';
import { useTranslation } from 'react-i18next';

import { LogsPanel } from '@/components/steps/LogsPanel';
import { SettingsPageShell } from './SettingsPageShell';

export const SettingsLogsPage: React.FC<{ standalone?: boolean }> = ({ standalone = false }) => {
  const { t } = useTranslation();

  if (standalone) {
    return (
      <div className="flex h-full flex-col gap-6">
        <div className="flex flex-col gap-1.5">
          <h1 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
            {t('settings.logsTitle')}
          </h1>
          <p className="text-[14px] leading-[1.55] text-muted">{t('settings.logsSubtitle')}</p>
        </div>
        <LogsPanel titleKey="settings.logsTitle" compactHeader />
      </div>
    );
  }

  return (
    <SettingsPageShell
      activeTab="diagnostics"
      title={t('settings.logsTitle')}
      subtitle={t('settings.logsSubtitle')}
      breadcrumb={
        <div className="flex items-center gap-2 font-medium">
          <span>{t('settings.tabs.serviceRoot')}</span>
          <span>›</span>
          <span>{t('settings.tabs.diagnostics')}</span>
          <span>›</span>
          <span className="text-mint-ink">{t('settings.logsTitle')}</span>
        </div>
      }
    >
      <LogsPanel titleKey="settings.logsTitle" />
    </SettingsPageShell>
  );
};
