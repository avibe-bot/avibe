// Remembers the inbox filter tab across visits. The remembered tab is resumed on
// re-entry, EXCEPT after a long absence — then it falls back to Unread, because
// coming back hours later you usually want to triage what's new, not resume an
// old "All" view. Mirrors the localStorage conventions used elsewhere in the UI
// (module-level key, best-effort try/catch).
export type InboxFilter = 'unread' | 'all';

const STORAGE_KEY = 'vibe-remote:inbox-filter';
// How long the user must be away from the inbox before re-entry resets to Unread.
export const INBOX_REVERT_AFTER_MS = 60 * 60 * 1000; // 1 hour

interface StoredInboxFilter {
  tab: InboxFilter;
  // Epoch ms when the user last left the inbox; 0 = currently here / never left.
  leftAt: number;
}

export function readInboxFilter(): StoredInboxFilter {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<StoredInboxFilter>;
      return {
        tab: parsed.tab === 'all' ? 'all' : 'unread',
        leftAt: typeof parsed.leftAt === 'number' ? parsed.leftAt : 0,
      };
    }
  } catch {
    // Best-effort persistence only (private mode / SSR / corrupt value).
  }
  return { tab: 'unread', leftAt: 0 };
}

export function writeInboxFilter(tab: InboxFilter, leftAt: number): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ tab, leftAt }));
  } catch {
    // Best-effort persistence only.
  }
}

// Decide which tab to show on (re)entry: resume the remembered tab, but fall back
// to Unread once the user has been away longer than INBOX_REVERT_AFTER_MS. Pure,
// so the time-based behavior is easy to reason about.
export function resolveInboxFilter(stored: StoredInboxFilter, now: number): InboxFilter {
  if (stored.leftAt > 0 && now - stored.leftAt > INBOX_REVERT_AFTER_MS) return 'unread';
  return stored.tab;
}
