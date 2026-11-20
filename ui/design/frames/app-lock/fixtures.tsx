// The General page as App Lock's frames draw it: the shipped page shell around the shipped panel.
import { useTranslation } from 'react-i18next';

import { AppLockSettingView, type AppLockSettingActivity } from '@/components/settings/AppLockSetting';
import { SettingsPageShell } from '@/components/settings/SettingsPageShell';
import type { AppLockDevice } from '@/lib/appLock';
import type { AppLockStatus } from '@/lib/appLockContract';

export function General(props: { device: AppLockDevice; status: AppLockStatus; activity?: AppLockSettingActivity }) {
  const { t } = useTranslation();
  return (
    <SettingsPageShell title={t('settings.general.title')} subtitle={t('settings.general.subtitle')} activeTab="general" titleScale="landing">
      <div className="flex flex-col gap-6">
        <AppLockSettingView {...props} />
      </div>
    </SettingsPageShell>
  );
}
