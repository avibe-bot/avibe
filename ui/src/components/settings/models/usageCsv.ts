import type { UsageBucketRow, UsageReport } from './types';
import {
  filterBucketRows,
  modelLabel,
  pairKey,
  sourceIdentityLabel,
  sourceLabel,
  usageLabelContext,
  usageNonCachedInput,
  usageTokensAreKnown,
  usageTotalTokens,
  type UsageFilter,
  type UsageIdentity,
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
  sourceLabel: string;
  modelLabel: string;
  requests: string;
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
  const labelContext = usageLabelContext(report);
  const buckets = pinnedKey === null
    ? report.buckets
    : report.buckets.filter((bucket) => bucket.key === pinnedKey);
  const lines = buckets.flatMap((bucket) => {
    const rows = filterBucketRows(bucket, filter);
    const exportRows: Array<UsageBucketRow | null> = rows.length > 0 ? rows : [null];
    return exportRows.map((row) => {
      const knownTokens = row !== null && usageTokensAreKnown(row);
      const identity = row === null ? null : {
        key: pairKey(row.source_id, row.model_id),
        sourceId: row.source_id,
        modelId: row.model_id,
        sourceLabel: sourceLabel(report, row.source_id),
        modelLabel: modelLabel(report, row.source_id, row.model_id),
      } satisfies UsageIdentity;
      return [
        bucket.key,
        bucket.start_at,
        bucket.end_at,
        String(bucket.history_complete),
        row?.source_id ?? '',
        row?.model_id ?? '',
        row === null ? '' : sourceIdentityLabel(report, row.source_id, labelContext),
        row === null ? '' : identity?.modelLabel || unknownModel,
        row?.requests ?? (bucket.history_complete ? 0 : ''),
        knownTokens ? row.input_tokens : '',
        knownTokens ? usageNonCachedInput(row) : '',
        knownTokens ? row.cached_input_tokens : '',
        knownTokens ? row.output_tokens : '',
        knownTokens ? usageTotalTokens(row) : '',
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
    headers.sourceLabel,
    headers.modelLabel,
    headers.requests,
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
