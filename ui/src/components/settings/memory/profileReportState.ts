import type { MemoryProfileReportLanguage } from '../../../context/ApiContext';

/** Normalize the UI locale to the closed report-language contract. */
export const profileReportLanguage = (language: string): MemoryProfileReportLanguage =>
  language.toLowerCase().startsWith('zh') ? 'zh' : 'en';

/**
 * Reports are transient and belong to the exact deterministic profile snapshot
 * and language that triggered them. A provider timestamp, when present, is a
 * second identity fence against a server-side profile refresh between reads.
 */
export const profileReportRequestKey = (
  profileRevision: number,
  language: MemoryProfileReportLanguage,
): string => `${profileRevision}:${language}`;

/** A late report may only update the still-current profile revision and language. */
export const acceptsProfileReportCompletion = (requestKey: string, currentKey: string): boolean =>
  requestKey === currentKey;

/** A returned report must name the snapshot currently shown when it has one. */
export const acceptsProfileReportSnapshot = (
  expectedUpdatedAt: string | null,
  sourceProfileUpdatedAt: string | null | undefined,
): boolean => expectedUpdatedAt === null || sourceProfileUpdatedAt === expectedUpdatedAt;
