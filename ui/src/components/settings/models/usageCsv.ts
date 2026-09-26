import type { UsageBucketRow, UsageReport } from './types';
import {
  filterBucketRows,
  emptyCounters,
  modelLabel,
  usageNonCachedInput,
  usageTokensAreKnown,
  usageTotalTokens,
  type UsageFilter,
} from './usageProjection';

export function csvCell(value: string | number): string {
  if (typeof value === 'number') return String(value);
  let text = value;
  if (/^[=+\-@]/.test(text) || /^[\t\r]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export type UsageCsvHeaders = {
  bucketKey: string;
  startAt: string;
  endAt: string;
  historyComplete: string;
  sourceId: string;
  modelId: string;
  ledgerKey: string;
  sourceLabel: string;
  modelLabel: string;
  requests: string;
  tokenReports: string;
  inputTokens: string;
  nonCachedInputTokens: string;
  cachedInputTokens: string;
  outputTokens: string;
  totalTokens: string;
};

export function buildUsageCsv(
  report: UsageReport,
  filter: UsageFilter,
  pinnedKey: string | null,
  headers: UsageCsvHeaders,
  unknownModel: string,
): string {
  // The export keeps every row under the IDs it was metered under; a label is
  // what config names that ID, and a Source config let go reads as its ID.
  const sourceLabel = (sourceId: string) => report.sources
    .find((source) => source.source_id === sourceId)?.label?.trim() || sourceId;
  const buckets = pinnedKey === null
    ? report.buckets
    : report.buckets.filter((bucket) => bucket.key === pinnedKey);
  const lines = buckets.flatMap((bucket) => {
    const rows = filterBucketRows(bucket, filter);
    const exportRows: Array<UsageBucketRow | null> = rows.length > 0 ? rows : [null];
    return exportRows.map((row) => {
      const counters = row ?? (bucket.history_complete ? emptyCounters() : null);
      const knownTokens = counters !== null && usageTokensAreKnown(counters);
      const label = row === null ? null : modelLabel(report, row.source_id, row.model_id);
      return [
        bucket.key,
        bucket.start_at,
        bucket.end_at,
        String(bucket.history_complete),
        row?.source_id ?? '',
        label ?? '',
        row?.model_id ?? '',
        row === null ? '' : sourceLabel(row.source_id),
        row === null ? '' : label || unknownModel,
        row?.requests ?? (bucket.history_complete ? 0 : ''),
        row?.token_reports ?? (bucket.history_complete ? 0 : ''),
        knownTokens ? counters.input_tokens : '',
        knownTokens ? usageNonCachedInput(counters) : '',
        knownTokens ? counters.cached_input_tokens : '',
        knownTokens ? counters.output_tokens : '',
        knownTokens ? usageTotalTokens(counters) : '',
      ] as Array<string | number>;
    });
  });
  const headerLine = [
    headers.bucketKey,
    headers.startAt,
    headers.endAt,
    headers.historyComplete,
    headers.sourceId,
    headers.modelId,
    headers.ledgerKey,
    headers.sourceLabel,
    headers.modelLabel,
    headers.requests,
    headers.tokenReports,
    headers.inputTokens,
    headers.nonCachedInputTokens,
    headers.cachedInputTokens,
    headers.outputTokens,
    headers.totalTokens,
  ];
  return [headerLine, ...lines]
    .map((line) => line.map(csvCell).join(','))
    .join('\r\n');
}
