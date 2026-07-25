// Remembers which Model Hub migration configs the user dismissed, so the banner
// stays non-nagging. Stores the dismissed set of per-config identities (stable
// scan id + proposed action — the backend derives ids as content hashes,
// `migration.py` sha256[:16]). The banner stays hidden while the current
// importable set is a SUBSET of what was dismissed — so importing or removing one
// config from a dismissed batch does NOT re-nag — and resurfaces only when a
// genuinely new id/action appears. Mirrors the localStorage conventions used
// elsewhere (module-level key, best-effort try/catch, pure helpers).
import type { MigrationItem } from '@/components/settings/models/types';

const STORAGE_KEY = 'vibe-remote:model-hub-migration-dismissed';

const identity = (item: MigrationItem): string => `${item.id}:${item.proposed_action}`;

/** Record the current importable set as dismissed. */
export function writeMigrationDismissed(items: MigrationItem[]): void {
  try {
    const ids = [...new Set(items.map(identity))].sort();
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // Best-effort persistence only (private mode / SSR / blocked storage).
  }
}

/** True when every current importable config was already dismissed (subset of the
 *  dismissed set) — i.e. nothing genuinely new to surface. */
export function isMigrationDismissed(items: MigrationItem[]): boolean {
  if (items.length === 0) return false;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return false;
    const dismissed = new Set<string>(JSON.parse(raw));
    return items.every((i) => dismissed.has(identity(i)));
  } catch {
    return false;
  }
}
