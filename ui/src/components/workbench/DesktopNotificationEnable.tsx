import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Bell } from 'lucide-react';

import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { desktopNotificationsActions, listenDesktopNotifications } from '../../lib/desktopShell';

/** Reuses the existing local Workbench contract; a rejected probe grants nothing. */
export function DesktopNotificationEnable() {
  const { t } = useTranslation();
  const [actions] = useState(desktopNotificationsActions);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [denied, setDenied] = useState(false);
  const [unavailable, setUnavailable] = useState(!actions);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const revision = useRef(0);

  useEffect(() => {
    if (!actions) return;
    let active = true;
    const refresh = async () => {
      const read = ++revision.current;
      try {
        const [value, authorization] = await Promise.all([
          actions.get(),
          actions.authorization().catch(() => null),
        ]);
        if (active && read === revision.current) {
          setEnabled(value);
          setDenied(authorization === 'denied');
          setUnavailable(false);
        }
      } catch {
        if (active && read === revision.current) {
          setEnabled(null);
          setUnavailable(true);
        }
      }
    };
    const stop = listenDesktopNotifications(() => { void refresh(); });
    const visible = () => { if (document.visibilityState === 'visible') void refresh(); };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', visible);
    void refresh();
    return () => {
      active = false;
      revision.current++;
      stop();
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [actions]);

  const enable = async () => {
    if (!actions || saving) return;
    setSaving(true);
    setFailed(false);
    ++revision.current;
    try {
      // The shell persists the preference and updates its native menu first.
      // Read it back; never infer success from the click or navigate elsewhere.
      await actions.set(true);
      setEnabled(await actions.get());
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col items-start gap-1.5">
      <span className="text-[11px] text-muted">{t('workbench.notifications.device')}</span>
      {unavailable ? (
        <span className="text-xs text-muted">{t('settings.notifications.desktopHintMenuBar')}</span>
      ) : denied ? (
        <span role="status" className="text-xs text-muted">{t('settings.notifications.desktopDenied')}</span>
      ) : enabled === null ? (
        <span className="text-xs text-muted">{t('workbench.inbox.notifications.checking')}</span>
      ) : enabled ? (
        <Badge variant="success" className="h-8 rounded-lg px-3">
          <Bell className="size-3" />{t('workbench.inbox.notifications.enabled')}
        </Badge>
      ) : (
        <Button variant="secondary" size="xs" disabled={saving} onClick={() => void enable()}>
          <Bell className="size-3.5" />{t('workbench.inbox.notifications.enable')}
        </Button>
      )}
      {failed && <span role="alert" className="text-xs text-muted">{t('settings.notifications.desktopSaveFailed')}</span>}
    </div>
  );
}
