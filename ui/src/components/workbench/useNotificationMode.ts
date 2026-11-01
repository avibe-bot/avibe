import { Bell, BellMinus, BellOff } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { useBots } from '../../context/BotsContext';
import { useNotificationPreferences } from '../../context/NotificationPreferencesContext';
import { notificationMode, type NotificationMode } from '../../lib/notificationPreferences';

const MODE_ICON: Record<NotificationMode, LucideIcon> = {
  all: Bell,
  bots: BellMinus,
  sessions: BellMinus,
  off: BellOff,
};

/** The caller's notification mode, and the bell that pictures it. */
export function useNotificationMode(): { mode: NotificationMode; icon: LucideIcon } {
  const { preferences } = useNotificationPreferences();
  const { enabled: hasBots } = useBots();
  const mode = notificationMode(preferences, hasBots);
  return { mode, icon: MODE_ICON[mode] };
}
