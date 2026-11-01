// Notification preferences and Inbox categories (docs/plans/workbench-refinements.md
// §1). The pure half: what the preference record looks like, which category a
// conversation is in, and the badge those two decide. Every surface that counts
// or switches notifications reads these, so the badge on the sidebar, the app
// icon and the tab favicon cannot disagree.
import { isBotSessionId } from './bots';

/** The account's switches. A category is loud while the master switch and its own are on. */
export type NotificationPreferences = {
  enabled: boolean;
  bots: boolean;
  sessions: boolean;
};

export const DEFAULT_NOTIFICATION_PREFERENCES: NotificationPreferences = {
  enabled: true,
  bots: true,
  sessions: true,
};

export type InboxCategory = 'bot' | 'session';

/** Settings › Notifications, the full page the compact menus link to. */
export const NOTIFICATIONS_SETTINGS_PATH = '/settings/notifications';

/**
 * A Bot's permanent Session (`ses_<handle>`) is a Bot conversation; everything
 * else is an ordinary Session. The `ses_` namespace belongs to Bots alone
 * (docs/plans/bots.md D2), and the Sessions of removed Bots never reach an
 * Inbox read, so the id alone settles it for every Session the Inbox can hold.
 */
export const inboxCategory = (sessionId: string): InboxCategory => (isBotSessionId(sessionId) ? 'bot' : 'session');

export const isCategoryLoud = (preferences: NotificationPreferences, category: InboxCategory): boolean =>
  preferences.enabled && (category === 'bot' ? preferences.bots : preferences.sessions);

export type UnreadByCategory = Record<InboxCategory, number>;

export function unreadByCategory(unreadBySession: Record<string, number>): UnreadByCategory {
  const counts: UnreadByCategory = { bot: 0, session: 0 };
  for (const [sessionId, count] of Object.entries(unreadBySession)) {
    if (count > 0) counts[inboxCategory(sessionId)] += count;
  }
  return counts;
}

/** The aggregate badge: unread in loud categories only. With every switch on it is the whole unread total. */
export const badgeTotal = (counts: UnreadByCategory, preferences: NotificationPreferences): number =>
  (isCategoryLoud(preferences, 'bot') ? counts.bot : 0) + (isCategoryLoud(preferences, 'session') ? counts.session : 0);

/**
 * What the switches amount to, for the one-word labels. Without Bots there is
 * one category, so the record reads as all or off.
 */
export type NotificationMode = 'all' | 'bots' | 'sessions' | 'off';

export function notificationMode(preferences: NotificationPreferences, hasBots: boolean): NotificationMode {
  const sessions = isCategoryLoud(preferences, 'session');
  if (!hasBots) return sessions ? 'all' : 'off';
  const bots = isCategoryLoud(preferences, 'bot');
  if (bots && sessions) return 'all';
  if (bots) return 'bots';
  if (sessions) return 'sessions';
  return 'off';
}

/** A read or write response: the record itself or under `preferences`. A missing switch is on. */
export function parseNotificationPreferences(payload: unknown): NotificationPreferences | null {
  if (!payload || typeof payload !== 'object') return null;
  const outer = payload as Record<string, unknown>;
  if (outer.ok === false) return null;
  const record = outer.preferences && typeof outer.preferences === 'object'
    ? (outer.preferences as Record<string, unknown>)
    : outer;
  const flag = (key: keyof NotificationPreferences) => record[key] !== false;
  return { enabled: flag('enabled'), bots: flag('bots'), sessions: flag('sessions') };
}
