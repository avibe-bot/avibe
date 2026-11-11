import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useBots } from '@/context/BotsContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useNotificationPreferences } from '@/context/NotificationPreferencesContext';
import {
  desktopNotificationsActions,
  isDesktopShell,
  listenDesktopNotifications,
  type DesktopNotificationsActions,
} from '@/lib/desktopShell';
import { hasNativeCapability } from '@/lib/native';
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
  const nativePush = hasNativeCapability('push');
  const [desktopActions, setDesktopActions] = useState<DesktopNotificationsActions | null>(null);
  const [desktopEnabled, setDesktopEnabled] = useState<boolean | null>(null);
  const [desktopSaving, setDesktopSaving] = useState(false);
  const [desktopSaveFailed, setDesktopSaveFailed] = useState(false);

  useEffect(() => {
    if (!desktop) return;
    const candidate = desktopNotificationsActions();
    let active = true;
    let probeSucceeded = false;
    const stopListening = listenDesktopNotifications((enabled) => {
      if (!active || !probeSucceeded) return;
      setDesktopEnabled(enabled);
      setDesktopSaveFailed(false);
    });
    if (!candidate) {
      return () => {
        active = false;
        stopListening();
      };
    }
    void candidate.get().then((enabled) => {
      if (!active) return;
      probeSucceeded = true;
      setDesktopActions(candidate);
      setDesktopEnabled(enabled);
    }).catch(() => {
      if (!active) return;
      setDesktopActions(null);
      setDesktopEnabled(null);
    });
    return () => {
      active = false;
      stopListening();
    };
  }, [desktop]);

  const desktopSwitchReady = desktopActions !== null && desktopEnabled !== null;
  const saveDesktopNotifications = async () => {
    if (!desktopActions || desktopEnabled === null || desktopSaving) return;
    setDesktopSaving(true);
    setDesktopSaveFailed(false);
    try {
      const saved = await desktopActions.set(!desktopEnabled);
      setDesktopEnabled(saved);
    } catch {
      setDesktopSaveFailed(true);
    } finally {
      setDesktopSaving(false);
    }
  };

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

      {(capabilities.can_read_instance || desktop) && (
        <SettingsPanel title={t('settings.notifications.deviceTitle')}>
          {capabilities.can_read_instance && !desktop && (
            <SettingsRow
              title={t(
                nativePush
                  ? 'settings.notifications.nativePushTitle'
                  : 'settings.notifications.pushTitle',
              )}
              description={t(
                nativePush
                  ? 'settings.notifications.nativePushHint'
                  : 'settings.notifications.pushHint',
              )}
              control={<WebPushControl />}
            />
          )}
          {desktop && (
            <SettingsRow
              title={t('settings.notifications.desktopTitle')}
              description={
                desktopSwitchReady && desktopSaveFailed
                  ? t('settings.notifications.desktopSaveFailed')
                  : desktopSwitchReady
                    ? t('settings.notifications.desktopHint')
                    : t('settings.notifications.desktopHintMenuBar')
              }
              control={desktopSwitchReady ? (
                <ToggleSwitch
                  enabled={desktopEnabled === true}
                  disabled={desktopSaving}
                  onClick={() => void saveDesktopNotifications()}
                  label={t('settings.notifications.desktopTitle')}
                />
              ) : undefined}
            />
          )}
        </SettingsPanel>
      )}
    </SettingsPageShell>
  );
};
