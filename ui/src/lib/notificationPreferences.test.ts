import { describe, expect, it } from 'vitest';

import {
  DEFAULT_NOTIFICATION_PREFERENCES,
  badgeTotal,
  notificationMode,
  parseNotificationPreferences,
  unreadByCategory,
} from './notificationPreferences';

const ALL_ON = DEFAULT_NOTIFICATION_PREFERENCES;
const BOTS_ONLY = { enabled: true, bots: true, sessions: false };
const SESSIONS_ONLY = { enabled: true, bots: false, sessions: true };
const MASTER_OFF = { enabled: false, bots: true, sessions: true };

describe('notification preferences', () => {
  // Two Bot replies in the main Bot's and a colleague's conversation, nine in
  // ordinary Sessions (generated ids never carry `_`).
  const unread = { ses_main: 1, ses_cso: 1, sesa1b2c3d4e5: 4, sesz9y8x7w6v5: 5, sesq1w2e3r4t5: 0 };
  const counts = unreadByCategory(unread);

  it('splits unread into Bot conversations and ordinary Sessions by Session id', () => {
    expect(counts).toEqual({ bot: 2, session: 9 });
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

  it('reads the record bare or wrapped, and treats a missing switch as on', () => {
    expect(parseNotificationPreferences({ enabled: true, bots: true, sessions: false })).toEqual(BOTS_ONLY);
    expect(parseNotificationPreferences({ ok: true, preferences: { sessions: false } })).toEqual(BOTS_ONLY);
    expect(parseNotificationPreferences({})).toEqual(ALL_ON);
    expect(parseNotificationPreferences({ ok: false, error: 'forbidden' })).toBeNull();
    expect(parseNotificationPreferences(null)).toBeNull();
  });
});
