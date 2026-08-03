import type { MemoryProfileReportLanguage } from '../../../context/ApiContext';

export type MemoryProfilePageFreshness = 'current' | 'stale' | 'unknown';

/** Normalize the UI locale to the closed profile-page language contract. */
export const profileReportLanguage = (language: string): MemoryProfileReportLanguage =>
  language.toLowerCase().startsWith('zh') ? 'zh' : 'en';

/** A late descriptor may update only the language that is still visible. */
export const acceptsProfilePageCompletion = (
  requested: MemoryProfileReportLanguage,
  current: MemoryProfileReportLanguage,
): boolean => requested === current;

/** Compare opaque source identities; timestamps are display metadata only. */
export const profilePageFreshness = (
  currentSnapshotId: string | null | undefined,
  sourceSnapshotId: string | null | undefined,
): MemoryProfilePageFreshness => {
  if (!currentSnapshotId || !sourceSnapshotId) return 'unknown';
  return currentSnapshotId === sourceSnapshotId ? 'current' : 'stale';
};
