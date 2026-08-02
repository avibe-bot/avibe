import type { MemoryProfileReportLanguage } from '../../../context/ApiContext';

/** Normalize the UI locale to the closed report-language contract. */
export const profileReportLanguage = (language: string): MemoryProfileReportLanguage =>
  language.toLowerCase().startsWith('zh') ? 'zh' : 'en';

/**
 * Reports are transient and belong to the exact deterministic profile snapshot
 * that triggered them, not to an optional provider timestamp.
 */
export const profileReportRequestKey = (
  profileRevision: number,
  language: MemoryProfileReportLanguage,
): string => `${profileRevision}:${language}`;

/** A late report may only update the still-current profile revision and language. */
export const acceptsProfileReportCompletion = (requestKey: string, currentKey: string): boolean =>
  requestKey === currentKey;
