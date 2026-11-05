// Whether the browser keeps the local message store under storage pressure, as
// the diagnostics page names it. The installed app asks for persistent storage
// silently; a plain browser tab is never asked (docs/plans/local-sync-store.md,
// decision D5), so ``null`` there means not asked rather than unknown.

export type OfflineHistoryPersistence = 'granted' | 'denied' | 'not-requested' | 'unknown';

export function offlineHistoryPersistence(persisted: boolean | null, standalone: boolean): OfflineHistoryPersistence {
  if (persisted === true) return 'granted';
  if (persisted === false) return 'denied';
  return standalone ? 'unknown' : 'not-requested';
}
