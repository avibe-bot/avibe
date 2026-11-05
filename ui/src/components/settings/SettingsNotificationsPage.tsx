import { useTranslation } from 'react-i18next';

import { useBots } from '@/context/BotsContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useNotificationPreferences } from '@/context/NotificationPreferencesContext';
import { isDesktopShell } from '@/lib/desktopShell';
import { isNativeShell } from '@/lib/native';
import { WebPushControl } from '../workbench/WebPushControl';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsPanel, SettingsRow, ToggleSwitch } from './SettingsPrimitives';

/**
 * Settings › Notifications (docs/plans/workbench-refinements.md §1): the account's
 * switches with what each one means, and this device's own push state. The
 * Inbox's notification menu is the compact form of the same switches.
 */
export const SettingsNotificationsPage: React.FC = () => {
  const { t } = useTranslation();
  const { preferences, update } = useNotificationPreferences();
  const { enabled: hasBots } = useBots();
  const { capabilities } = useInstanceAuthorization();
  const off = !preferences.enabled;
  const desktop = isDesktopShell();
  const native = isNativeShell();

  return (
    <SettingsPageShell
      title={t('settings.sections.notifications')}
      subtitle={t('settings.notifications.subtitle')}
      activeTab="notifications"
    >
      <SettingsPanel>
        <SettingsRow
          title={t('workbench.notifications.title')}
          description={t('settings.notifications.masterHint')}
          control={(
            <ToggleSwitch
              enabled={preferences.enabled}
              onClick={() => void update({ enabled: !preferences.enabled })}
              label={t('workbench.notifications.title')}
            />
          )}
        />
        {hasBots && (
          <>
            <SettingsRow
              title={t('workbench.notifications.bots')}
              description={t('settings.notifications.botsHint')}
              control={(
                <ToggleSwitch
                  enabled={preferences.bots}
                  disabled={off}
                  onClick={() => void update({ bots: !preferences.bots })}
                  label={t('workbench.notifications.bots')}
                />
              )}
            />
            <SettingsRow
              title={t('workbench.notifications.sessions')}
              description={t('settings.notifications.sessionsHint')}
              control={(
                <ToggleSwitch
                  enabled={preferences.sessions}
                  disabled={off}
                  onClick={() => void update({ sessions: !preferences.sessions })}
                  label={t('workbench.notifications.sessions')}
                />
              )}
            />
          </>
        )}
      </SettingsPanel>

      {((capabilities.can_read_instance && !native) || desktop) && (
        <SettingsPanel title={t('settings.notifications.deviceTitle')}>
          {capabilities.can_read_instance && !native && (
            <SettingsRow
              title={t('settings.notifications.pushTitle')}
              description={t('settings.notifications.pushHint')}
              control={<WebPushControl />}
            />
          )}
          {desktop && (
            <SettingsRow
              title={t('settings.notifications.desktopTitle')}
              description={t('settings.notifications.desktopHint')}
            />
          )}
        </SettingsPanel>
      )}
    </SettingsPageShell>
  );
};
