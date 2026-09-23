import type { MigrationItem } from '@/components/settings/models/types';

const STORAGE_KEY = 'vibe-remote:model-hub-migration-dismissed';
const identity = (item: MigrationItem): string => `${item.id}:${item.proposed_action}`;

const readDismissed = (): Set<string> => {
  try {
    return new Set<string>(JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '[]'));
  } catch {
    return new Set();
  }
};

export function writeMigrationDismissed(items: MigrationItem[]): void {
  if (items.length === 0) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...new Set([...readDismissed(), ...items.map(identity)])].sort()));
  } catch {
    // Setup remains usable when storage is unavailable.
  }
}

export function clearMigrationDismissed(items: MigrationItem[]): void {
  if (items.length === 0) return;
  try {
    const dismissed = readDismissed();
    for (const item of items) dismissed.delete(identity(item));
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...dismissed].sort()));
  } catch {
    // Explicit selection still applies to the current setup visit.
  }
}

export function isMigrationDismissed(items: MigrationItem[]): boolean {
  if (items.length === 0) return false;
  try {
    const dismissed = readDismissed();
    return items.every((item) => dismissed.has(identity(item)));
  } catch {
    return false;
  }
}
