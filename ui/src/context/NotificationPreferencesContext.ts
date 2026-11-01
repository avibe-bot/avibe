import { createContext, useContext } from 'react';

import { DEFAULT_NOTIFICATION_PREFERENCES, type NotificationPreferences } from '../lib/notificationPreferences';

// The caller's notification switches (docs/plans/workbench-refinements.md §1).
// `NotificationPreferencesProvider.tsx` owns the read, the stream nudge and the
// write; this module is the component-free handle consumers import.
export interface NotificationPreferencesState {
  preferences: NotificationPreferences;
  /** A record from the server is on screen. Until then the switches read as
   *  all on, so nothing that counts them should overwrite a badge the push
   *  worker set while the app was closed; a failed read keeps it so. */
  loaded: boolean;
  /** Apply a change at once and save only that switch; a refused save puts it
   *  back. Resolves whether the server kept it. */
  update: (patch: Partial<NotificationPreferences>) => Promise<boolean>;
}

// Outside a provider there is nothing to wait for, so the default reads as loaded.
const EMPTY: NotificationPreferencesState = {
  preferences: DEFAULT_NOTIFICATION_PREFERENCES,
  loaded: true,
  update: async () => false,
};

export const NotificationPreferencesContext = createContext<NotificationPreferencesState>(EMPTY);

/** The caller's switches. Outside the provider (a Show Page, a test harness) this
 *  is the all-on default, which is also what an account that never chose reads. */
export const useNotificationPreferences = (): NotificationPreferencesState => useContext(NotificationPreferencesContext);
