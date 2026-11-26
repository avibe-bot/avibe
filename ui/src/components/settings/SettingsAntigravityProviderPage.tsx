import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { SettingsPageShell } from './SettingsPageShell';
import { AntigravityProviderConfig } from './providers/AntigravityProviderConfig';

export const SettingsAntigravityProviderPage: React.FC = () => {
  const { t } = useTranslation();

  return (
    <SettingsPageShell
      activeTab="backends"
      title={t('settings.backends.antigravityTitle')}
      subtitle={t('settings.backends.antigravitySubtitle')}
      breadcrumb={
        <Link to="/settings/backends" className="inline-flex items-center gap-1.5 hover:text-foreground">
          <ArrowLeft className="size-3" />
          {t('settings.backends.backToBackends')}
        </Link>
      }
    >
      <AntigravityProviderConfig />
    </SettingsPageShell>
  );
};
