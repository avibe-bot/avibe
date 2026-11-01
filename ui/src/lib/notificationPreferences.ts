// Notification preferences and Inbox categories (docs/plans/workbench-refinements.md
// §1). The pure half: what the preference record looks like, which category a
// conversation is in, and the badge those two decide. Every surface that counts
// or switches notifications reads these, so the badge on the sidebar, the app
// icon and the tab favicon cannot disagree.
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
 * A Session is a Bot conversation while it is a current Bot's permanent Session,
 * the server's own definition (`core/notification_categories.py`). The caller
 * passes the Bot Sessions it knows, the live Bot list (`BotRosterContext`); a
 * removed Bot's `ses_` Session is therefore an ordinary Session, as it is on the
 * server. One classifier serves the feed rows and the unread map, so a row's
 * section and the count it adds to cannot disagree.
 */
export const inboxCategory = (sessionId: string, botSessionIds: ReadonlySet<string>): InboxCategory =>
  (botSessionIds.has(sessionId) ? 'bot' : 'session');

export const isCategoryLoud = (preferences: NotificationPreferences, category: InboxCategory): boolean =>
  preferences.enabled && (category === 'bot' ? preferences.bots : preferences.sessions);

export type UnreadByCategory = Record<InboxCategory, number>;

/** The unread map split by category: the server's `split_unread_counts`. */
export function unreadByCategory(
  unreadBySession: Record<string, number>,
  botSessionIds: ReadonlySet<string>,
): UnreadByCategory {
  const counts: UnreadByCategory = { bot: 0, session: 0 };
  for (const [sessionId, count] of Object.entries(unreadBySession)) {
    if (count > 0) counts[inboxCategory(sessionId, botSessionIds)] += count;
  }
  return counts;
}

/** The aggregate badge: unread in loud categories only, the server's `badge_total`.
 *  With every switch on it is the whole unread total. */
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

/** A read or write response, `{ok, preferences}`. A missing switch is on; a response without the record is none. */
export function parseNotificationPreferences(payload: unknown): NotificationPreferences | null {
  if (!payload || typeof payload !== 'object') return null;
  const outer = payload as Record<string, unknown>;
  if (outer.ok === false || !outer.preferences || typeof outer.preferences !== 'object') return null;
  const record = outer.preferences as Record<string, unknown>;
  const flag = (key: keyof NotificationPreferences) => record[key] !== false;
  return { enabled: flag('enabled'), bots: flag('bots'), sessions: flag('sessions') };
}
