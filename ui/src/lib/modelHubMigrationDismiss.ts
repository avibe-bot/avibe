// Remembers that the user dismissed the Model Hub migration banner, so it stays
// non-nagging. Keyed by a STABLE signature of what is importable (sorted
// `backend:kind` pairs) rather than a plain flag: dismissing hides exactly the
// configs the user saw, but a genuinely new importable config (a different
// backend/kind) changes the signature and resurfaces the banner once. Mirrors
// the localStorage conventions used elsewhere (module-level key, best-effort
// try/catch, pure helpers).
import type { MigrationItem } from '@/components/settings/models/types';

const STORAGE_KEY = 'vibe-remote:model-hub-migration-dismissed';

/** Stable signature of the importable set — order-independent, id-independent. */
export function migrationSignature(items: MigrationItem[]): string {
  return [...new Set(items.map((i) => `${i.backend}:${i.kind}`))].sort().join('|');
}

export function readMigrationDismissed(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // Best-effort only (private mode / SSR / blocked storage).
    return null;
  }
}

export function writeMigrationDismissed(signature: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, signature);
  } catch {
    // Best-effort persistence only.
  }
}
