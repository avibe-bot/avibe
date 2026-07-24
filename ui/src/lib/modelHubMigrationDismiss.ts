// Remembers that the user dismissed the Model Hub migration banner, so it stays
// non-nagging. Keyed by a STABLE signature of the importable set — the per-config
// scan ids (plus proposed action), which the backend derives as content hashes
// (`migration.py` sha256[:16]). Dismissing hides exactly the configs the user
// saw; a genuinely new importable config (e.g. a rotated key or an added
// provider) carries a new id, so the signature changes and the banner resurfaces
// once. Keying on `backend:kind` alone would collapse those distinct configs and
// suppress the banner forever, which is why we use identity here. Mirrors the
// localStorage conventions used elsewhere (module-level key, best-effort
// try/catch, pure helpers).
import type { MigrationItem } from '@/components/settings/models/types';

const STORAGE_KEY = 'vibe-remote:model-hub-migration-dismissed';

/** Stable, order-independent signature of the importable set by config identity. */
export function migrationSignature(items: MigrationItem[]): string {
  return [...new Set(items.map((i) => `${i.id}:${i.proposed_action}`))].sort().join('|');
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
