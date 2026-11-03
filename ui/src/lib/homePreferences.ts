// The account's home preferences (`/api/home/preferences`, core/home_preferences.py): when
// Getting started was put off until the next day, which of its items are not needed at all, when
// the tip of the day was hidden until the next day, and whether tips show at all.

/** A Getting started item, as the server names it. */
export type SetupItem = 'model' | 'notifications' | 'computer_use' | 'chat_apps' | 'phone';

export const SETUP_ITEMS: readonly SetupItem[] = ['model', 'notifications', 'computer_use', 'chat_apps', 'phone'];

export type HomePreferences = {
  /** The reader's local date ("YYYY-MM-DD") Getting started was put off on, if any. */
  setup_later_on: string | null;
  /** Items removed for good ("Not needed"); the server only ever adds to it. */
  setup_not_needed: SetupItem[];
  /** The local date the tip of the day was hidden on, if any. */
  tip_hidden_on: string | null;
  tips_enabled: boolean;
};

export const DEFAULT_HOME_PREFERENCES: HomePreferences = {
  setup_later_on: null,
  setup_not_needed: [],
  tip_hidden_on: null,
  tips_enabled: true,
};

/** The reader's own calendar date: "until tomorrow" is the reader's tomorrow. */
export const localDate = (date: Date): string => {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};

/** A read or write response, `{ok, preferences}`; a response without the record is none. */
export function parseHomePreferences(payload: unknown): HomePreferences | null {
  if (!payload || typeof payload !== 'object') return null;
  const outer = payload as Record<string, unknown>;
  if (outer.ok === false || !outer.preferences || typeof outer.preferences !== 'object') return null;
  const record = outer.preferences as Record<string, unknown>;
  const date = (value: unknown) => (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null);
  const notNeeded = Array.isArray(record.setup_not_needed)
    ? SETUP_ITEMS.filter((item) => (record.setup_not_needed as unknown[]).includes(item))
    : [];
  return {
    setup_later_on: date(record.setup_later_on),
    setup_not_needed: notNeeded,
    tip_hidden_on: date(record.tip_hidden_on),
    tips_enabled: record.tips_enabled !== false,
  };
}
