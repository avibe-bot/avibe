import type { MigrationItem } from '@/components/settings/models/types';

const STORAGE_KEY = 'vibe-remote:model-hub-migration-dismissed';
const identity = (item: MigrationItem): string => `${item.id}:${item.proposed_action}`;

export function writeMigrationDismissed(items: MigrationItem[]): void {
  if (items.length === 0) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...new Set(items.map(identity))].sort()));
  } catch {
    // Setup remains usable when storage is unavailable.
  }
}

export function clearMigrationDismissed(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Explicit selection still applies to the current setup visit.
  }
}

export function isMigrationDismissed(items: MigrationItem[]): boolean {
  if (items.length === 0) return false;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return false;
    const dismissed = new Set<string>(JSON.parse(raw));
    return items.every((item) => dismissed.has(identity(item)));
  } catch {
    return false;
  }
}
