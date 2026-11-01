import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { useApi } from './ApiContext';
import { NotificationPreferencesContext, type NotificationPreferencesState } from './NotificationPreferencesContext';
import { DEFAULT_NOTIFICATION_PREFERENCES, type NotificationPreferences } from '../lib/notificationPreferences';

/** Owns the caller's notification switches (`/api/notifications/preferences`).
 *
 *  Read on mount and re-read whenever the stream says this account's switches
 *  changed (on any of its devices), reconnects, or authorization moves: the
 *  broker replays nothing, and re-reading keeps one path that adopts a record.
 *
 *  A change shows at once and sends only the switch it changes, which the server
 *  merges, so two devices changing different switches keep both. Responses to
 *  overlapping writes cannot say which one the server applied last, so none of
 *  them is adopted whole: each confirms only the switches it wrote. A refused
 *  write puts its switches back to their last confirmed value, unless a later
 *  change has set them since, and once the last write of a burst settles the
 *  record is read back (a lone write's response is that read). Every read
 *  started before a write is fenced, so a slow read cannot put the old switches
 *  back. */
export const NotificationPreferencesProvider = ({ children }: { children: ReactNode }) => {
  const api = useApi();
  const [preferences, setPreferences] = useState<NotificationPreferences>(DEFAULT_NOTIFICATION_PREFERENCES);
  const [loaded, setLoaded] = useState(false);
  // The switches on screen, which the next change builds on (two quick toggles
  // must not lose the first while its save is in flight).
  const shownRef = useRef<NotificationPreferences>(DEFAULT_NOTIFICATION_PREFERENCES);
  const generationRef = useRef(0);
  // Writes in flight and writes since the last quiet moment; which change last
  // set each switch (a refused write may only undo a switch it still owns); and
  // each switch's last value the server confirmed, with the change that did.
  const pendingWritesRef = useRef(0);
  const burstWritesRef = useRef(0);
  const switchOwnerRef = useRef<Partial<Record<keyof NotificationPreferences, number>>>({});
  const confirmedRef = useRef<NotificationPreferences>(DEFAULT_NOTIFICATION_PREFERENCES);
  const confirmedByRef = useRef<Partial<Record<keyof NotificationPreferences, number>>>({});
  const confirm = useCallback((values: Partial<NotificationPreferences>, generation: number) => {
    for (const key of Object.keys(values) as (keyof NotificationPreferences)[]) {
      if ((confirmedByRef.current[key] ?? 0) > generation) continue;
      confirmedByRef.current[key] = generation;
      confirmedRef.current = { ...confirmedRef.current, [key]: values[key] };
    }
  }, []);
  const show = useCallback((next: NotificationPreferences) => {
    shownRef.current = next;
    setPreferences(next);
  }, []);

  const read = useCallback(async () => {
    const generation = ++generationRef.current;
    try {
      const next = await api.getNotificationPreferences();
      if (generation !== generationRef.current) return;
      confirm(next, generation);
      show(next);
      setLoaded(true);
    } catch {
      // Unreadable is not "all off": keep what we have, and stay unknown until a
      // record arrives, so the badges keep the number the push worker set.
    }
  }, [api, confirm, show]);

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
      const changed = Object.keys(patch) as (keyof NotificationPreferences)[];
      for (const key of changed) switchOwnerRef.current[key] = generation;
      pendingWritesRef.current += 1;
      burstWritesRef.current += 1;
      show({ ...shownRef.current, ...patch });
      let saved: NotificationPreferences | null = null;
      try {
        saved = await api.updateNotificationPreferences(patch);
        confirm(patch, generation);
      } catch {
        const undo = Object.fromEntries(
          changed
            .filter((key) => switchOwnerRef.current[key] === generation)
            .map((key) => [key, confirmedRef.current[key]]),
        );
        show({ ...shownRef.current, ...undo });
      }
      pendingWritesRef.current -= 1;
      if (pendingWritesRef.current === 0) {
        const alone = burstWritesRef.current === 1;
        burstWritesRef.current = 0;
        if (alone && saved && generation === generationRef.current) {
          confirm(saved, generation);
          show(saved);
          setLoaded(true);
        } else {
          void read();
        }
      }
      return saved !== null;
    },
    [api, confirm, read, show],
  );

  const value = useMemo<NotificationPreferencesState>(
    () => ({ preferences, loaded, update }),
    [preferences, loaded, update],
  );

  return <NotificationPreferencesContext.Provider value={value}>{children}</NotificationPreferencesContext.Provider>;
};
