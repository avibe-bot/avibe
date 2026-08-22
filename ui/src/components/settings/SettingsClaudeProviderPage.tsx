import React from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft } from 'lucide-react';

import { SettingsPageShell } from './SettingsPageShell';
import { ClaudeProviderConfig } from './providers/ClaudeProviderConfig';

export const SettingsClaudeProviderPage: React.FC = () => {
  const { t } = useTranslation();

  return (
    <SettingsPageShell
      activeTab="backends"
      title={t('settings.backends.claudeTitle')}
      subtitle={t('settings.backends.claudeSubtitle')}
      breadcrumb={
        <Link to="/settings/backends" className="inline-flex items-center gap-1.5 hover:text-foreground">
          <ArrowLeft className="size-3" />
          {t('settings.backends.codexBackToBackends')}
        </Link>
      }
    >
      <ClaudeProviderConfig />
    </SettingsPageShell>
  );
};
