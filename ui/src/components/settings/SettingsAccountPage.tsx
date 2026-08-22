import React from 'react';
import { LogOut } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Button } from '@/components/ui/button';
import { useAuthAccount } from '@/lib/useAuthAccount';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsPanel, SettingsRow } from './SettingsPrimitives';

export const SettingsAccountPage: React.FC = () => {
  const { t } = useTranslation();
  const { email, signingOut, signOut } = useAuthAccount();

  return (
    <SettingsPageShell
      activeTab="account"
      title={t('settings.accountTitle')}
      subtitle={t('settings.accountSubtitle')}
    >
      <SettingsPanel title={t('settings.identityTitle')}>
        <SettingsRow
          title={t('settings.signedInIdentity')}
          description={email ?? t('settings.localSession')}
          control={email ? (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={signingOut}
              onClick={() => void signOut()}
              className={clsx(signingOut && 'opacity-70')}
            >
              <LogOut className="size-3.5" />
              {signingOut ? t('appShell.signingOut') : t('appShell.signOut')}
            </Button>
          ) : null}
        />
      </SettingsPanel>
    </SettingsPageShell>
  );
};
