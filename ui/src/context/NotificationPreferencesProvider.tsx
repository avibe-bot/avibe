import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { useApi } from './ApiContext';
import { NotificationPreferencesContext, type NotificationPreferencesState } from './NotificationPreferencesContext';
import { DEFAULT_NOTIFICATION_PREFERENCES, type NotificationPreferences } from '../lib/notificationPreferences';

/** Owns the caller's notification switches (`/api/notifications/preferences`).
 *
 *  Read on mount and re-read whenever the stream says some account's switches
 *  changed, reconnects, or authorization moves: the broker replays nothing, and
 *  its preference event is a nudge shared by every reader rather than the record.
 *  A write applies optimistically and every read started before it is fenced, so
 *  a slow read cannot put the old switches back. */
export const NotificationPreferencesProvider = ({ children }: { children: ReactNode }) => {
  const api = useApi();
  const [preferences, setPreferences] = useState<NotificationPreferences>(DEFAULT_NOTIFICATION_PREFERENCES);
  const [loaded, setLoaded] = useState(false);
  // The last record the server confirmed, which a refused write restores, and
  // the one on screen, which the next change builds on (two quick toggles must
  // not lose the first while its save is in flight).
  const savedRef = useRef<NotificationPreferences>(DEFAULT_NOTIFICATION_PREFERENCES);
  const shownRef = useRef<NotificationPreferences>(DEFAULT_NOTIFICATION_PREFERENCES);
  const generationRef = useRef(0);
  const show = useCallback((next: NotificationPreferences) => {
    shownRef.current = next;
    setPreferences(next);
  }, []);

  const read = useCallback(async () => {
    const generation = ++generationRef.current;
    try {
      const next = await api.getNotificationPreferences();
      if (generation !== generationRef.current) return;
      savedRef.current = next;
      show(next);
    } catch {
      // Unreadable is not "all off": keep what we have, defaults included.
    } finally {
      if (generation === generationRef.current) setLoaded(true);
    }
  }, [api, show]);

  useEffect(() => {
    // The first read does not wait for the stream: one that never connects must
    // not leave the switches unread.
    queueMicrotask(() => void read());
    return api.connectWorkbenchEvents({
      onConnected: () => void read(),
      onNotificationPreferencesChanged: () => void read(),
      onAuthorizationChanged: () => void read(),
    });
  }, [api, read]);

  const update = useCallback(
    async (patch: Partial<NotificationPreferences>) => {
      const generation = ++generationRef.current;
      const next = { ...shownRef.current, ...patch };
      show(next);
      try {
        const saved = await api.updateNotificationPreferences(next);
        savedRef.current = saved;
        if (generation === generationRef.current) {
          show(saved);
          setLoaded(true);
        }
        return true;
      } catch {
        if (generation === generationRef.current) show(savedRef.current);
        return false;
      }
    },
    [api, show],
  );

  const value = useMemo<NotificationPreferencesState>(
    () => ({ preferences, loaded, update }),
    [preferences, loaded, update],
  );

  return <NotificationPreferencesContext.Provider value={value}>{children}</NotificationPreferencesContext.Provider>;
};
