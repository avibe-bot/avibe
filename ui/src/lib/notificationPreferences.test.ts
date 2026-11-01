import { describe, expect, it } from 'vitest';

import {
  DEFAULT_NOTIFICATION_PREFERENCES,
  badgeTotal,
  notificationMode,
  parseNotificationPreferences,
  unreadByCategory,
} from './notificationPreferences';
import badgeFixture from './notificationBadge.fixture.json';

const ALL_ON = DEFAULT_NOTIFICATION_PREFERENCES;
const BOTS_ONLY = { enabled: true, bots: true, sessions: false };
const SESSIONS_ONLY = { enabled: true, bots: false, sessions: true };
const MASTER_OFF = { enabled: false, bots: true, sessions: true };

describe('notification preferences', () => {
  // Two Bot replies in the main Bot's and a colleague's conversation, nine in
  // ordinary Sessions, one of them a removed Bot's.
  const unread = { ses_main: 1, ses_cso: 1, ses_retired: 4, sesz9y8x7w6v5: 5, sesq1w2e3r4t5: 0 };
  const counts = unreadByCategory(unread, new Set(['ses_main', 'ses_cso']));

  it("splits unread by the live Bot list, not by the id's prefix", () => {
    expect(counts).toEqual({ bot: 2, session: 9 });
  });

  // The same fixture runs against the server's split_unread_counts and badge_total
  // (tests/test_notification_badge_parity.py), so the sidebar number and the app
  // icon or Dock badge the server computes cannot follow different rules.
  it.each(badgeFixture.cases)('computes the server\'s badge_total from the same data ($name)', (fixture) => {
    const split = unreadByCategory(badgeFixture.unread_by_session, new Set(fixture.bot_session_ids));
    expect(split).toEqual(fixture.unread_by_category);
    expect(badgeTotal(split, fixture.preferences)).toBe(fixture.badge_total);
  });

  it.each([
    ['every switch on: the whole total, as before', ALL_ON, 11],
    ['Bots only', BOTS_ONLY, 2],
    ['Sessions only', SESSIONS_ONLY, 9],
    ['master off, whatever the categories say', MASTER_OFF, 0],
  ])('badges loud categories only (%s)', (_label, preferences, expected) => {
    expect(badgeTotal(counts, preferences)).toBe(expected);
  });

  it('names the mode, with one category for a caller without Bots', () => {
    expect(notificationMode(ALL_ON, true)).toBe('all');
    expect(notificationMode(BOTS_ONLY, true)).toBe('bots');
    expect(notificationMode(SESSIONS_ONLY, true)).toBe('sessions');
    expect(notificationMode(MASTER_OFF, true)).toBe('off');
    expect(notificationMode({ enabled: true, bots: false, sessions: false }, true)).toBe('off');
    // Without Bots the Bot switch is not theirs to see, so it cannot read as partial.
    expect(notificationMode(SESSIONS_ONLY, false)).toBe('all');
    expect(notificationMode(MASTER_OFF, false)).toBe('off');
  });

  it('reads the wrapped record, and treats a missing switch as on', () => {
    expect(parseNotificationPreferences({ ok: true, preferences: { enabled: true, bots: true, sessions: false } })).toEqual(BOTS_ONLY);
    expect(parseNotificationPreferences({ ok: true, preferences: { sessions: false } })).toEqual(BOTS_ONLY);
    expect(parseNotificationPreferences({ ok: true, preferences: {} })).toEqual(ALL_ON);
    // A body without the record says nothing about the switches, least of all "all on".
    expect(parseNotificationPreferences({ ok: true })).toBeNull();
    expect(parseNotificationPreferences({ enabled: false })).toBeNull();
    expect(parseNotificationPreferences({ ok: false, error: 'forbidden' })).toBeNull();
    expect(parseNotificationPreferences(null)).toBeNull();
  });
});
