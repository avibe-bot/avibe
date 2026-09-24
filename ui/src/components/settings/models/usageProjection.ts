import type {
  UsageBucket,
  UsageBucketRow,
  UsageCounters,
  UsageReport,
  UsageWindowKey,
} from './types';

export const USAGE_WINDOW_OPTIONS = ['24h', '7d', '30d', '60d'] as const satisfies readonly UsageWindowKey[];
export type UsageWindowOption = UsageWindowKey;

export type UsageMetric = 'tokens' | 'input' | 'output' | 'cache' | 'requests';
export type UsageGroup = 'total' | 'type' | 'model' | 'source';
export type UsageFilter = {
  sourceIds: readonly string[];
  modelKeys: readonly string[];
};

export type UsageIdentity = {
  sourceId: string;
  modelId: string;
  sourceLabel: string;
  modelLabel: string | null;
  key: string;
};

export type UsageLabelContext = {
  sourceCollisionLabels: ReadonlySet<string>;
  identityCollisionLabels: ReadonlySet<string>;
  identityModelCollisionKeys: ReadonlySet<string>;
};

export type UsageSeries = {
  key: string;
  label: string;
  colorIndex: number;
  values: Array<number | null>;
};

export const PAIR_SEPARATOR = '\u0000';

export const pairKey = (sourceId: string, modelId: string): string =>
  `${sourceId}${PAIR_SEPARATOR}${modelId}`;

export const emptyCounters = (): UsageCounters => ({
  requests: 0,
  token_reports: 0,
  input_tokens: 0,
  cached_input_tokens: 0,
  output_tokens: 0,
});

export function aggregateCounters(rows: readonly UsageCounters[]): UsageCounters {
  return rows.reduce((total, row) => ({
    requests: total.requests + row.requests,
    token_reports: total.token_reports + row.token_reports,
    input_tokens: total.input_tokens + row.input_tokens,
    cached_input_tokens: total.cached_input_tokens + row.cached_input_tokens,
    output_tokens: total.output_tokens + row.output_tokens,
  }), emptyCounters());
}

export function usageTotalTokens(counters: UsageCounters): number {
  return counters.input_tokens + counters.output_tokens;
}

export function usageNonCachedInput(counters: UsageCounters): number {
  return Math.max(0, counters.input_tokens - counters.cached_input_tokens);
}

export function usageReportShortfall(counters: UsageCounters): number {
  return Math.max(0, counters.requests - counters.token_reports);
}

export function usageTokensAreKnown(counters: Pick<UsageCounters, 'requests' | 'token_reports'>): boolean {
  return counters.token_reports > 0 || counters.requests === 0;
}

export function usageTokensAreReported(counters: Pick<UsageCounters, 'token_reports'>): boolean {
  return counters.token_reports > 0;
}

export function usageMetricValue(counters: UsageCounters, metric: UsageMetric): number | null {
  if (metric === 'requests') return counters.requests;
  if (!usageTokensAreKnown(counters)) return null;
  if (metric === 'tokens') return usageTotalTokens(counters);
  if (metric === 'input') return counters.input_tokens;
  if (metric === 'output') return counters.output_tokens;
  return counters.cached_input_tokens;
}

export function usageCachedInputShare(counters: UsageCounters): number | null {
  if (counters.input_tokens <= 0 || !usageTokensAreKnown(counters)) return null;
  return Math.min(1, Math.max(0, counters.cached_input_tokens / counters.input_tokens));
}

export function usageIsEmpty(report: UsageReport): boolean {
  return report.buckets.every((bucket) => bucket.history_complete && bucket.rows.length === 0);
}

export function formatRfc3339(value: string, locale: string, includeOffset = false): string {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!match) return value;
  const [, year, month, day, hour, minute] = match;
  const suffix = includeOffset ? ` ${formatOffset(value)}` : '';
  if (locale.startsWith('zh')) return `${year}年${Number(month)}月${Number(day)}日 ${hour}:${minute}${suffix}`;
  const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
  const monthLabel = new Intl.DateTimeFormat(locale, { month: 'short', timeZone: 'UTC' }).format(date);
  return `${monthLabel} ${Number(day)}, ${year}, ${hour}:${minute}${suffix}`;
}

export function formatOffset(value: string): string {
  const match = value.match(/(Z|[+-]\d{2}:\d{2})$/);
  if (!match || match[1] === 'Z') return 'UTC';
  return `UTC${match[1]}`;
}

export function formatBucketLabel(bucket: UsageBucket, locale: string): string {
  if (bucket.key.match(/^\d{4}-\d{2}-\d{2}$/)) {
    const [year, month, day] = bucket.key.split('-').map(Number);
    if (locale.startsWith('zh')) return `${year}年${month}月${day}日`;
    return new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
      .format(new Date(Date.UTC(year, month - 1, day)));
  }
  return formatRfc3339(bucket.start_at, locale);
}

export function formatBucketAxisLabel(bucket: UsageBucket, locale: string): string {
  if (bucket.key.match(/^\d{4}-\d{2}-\d{2}$/)) {
    const [year, month, day] = bucket.key.split('-').map(Number);
    return new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', timeZone: 'UTC' })
      .format(new Date(Date.UTC(year, month - 1, day)));
  }
  const match = bucket.start_at.match(/T\d{2}:(\d{2})/);
  if (match) {
    const hour = bucket.start_at.slice(11, 13);
    return `${hour}:${match[1]}`;
  }
  return formatBucketLabel(bucket, locale);
}

export function formatBucketRange(bucket: UsageBucket, locale: string, includeOffsets = false): string {
  return `${formatRfc3339(bucket.start_at, locale, includeOffsets)} – ${formatRfc3339(bucket.end_at, locale, includeOffsets)}`;
}

export function sourceLabel(report: UsageReport, sourceId: string): string {
  const source = report.sources.find((candidate) => candidate.source_id === sourceId);
  return source?.label?.trim() || sourceId;
}

export function modelLabel(report: UsageReport, sourceId: string, modelId: string): string | null {
  const source = report.sources.find((candidate) => candidate.source_id === sourceId);
  const model = source?.models.find((candidate) => candidate.model_id === modelId);
  return model?.label?.trim() || null;
}

export function usageIdentities(report: UsageReport, rows: readonly UsageBucketRow[]): UsageIdentity[] {
  const keys = new Set(rows.map((row) => pairKey(row.source_id, row.model_id)));
  return [...keys].map((key) => {
    const separator = key.indexOf(PAIR_SEPARATOR);
    const sourceId = key.slice(0, separator);
    const modelId = key.slice(separator + 1);
    return {
      key,
      sourceId,
      modelId,
      sourceLabel: sourceLabel(report, sourceId),
      modelLabel: modelLabel(report, sourceId, modelId),
    };
  });
}

export function identityLabel(identity: UsageIdentity, locale = 'en'): string {
  const model = identity.modelLabel || (locale.startsWith('zh') ? '未知模型' : 'Unknown model');
  return `${identity.sourceLabel} · ${model}`;
}

export function usageLabelContext(
  report: UsageReport,
  rows: readonly UsageBucketRow[] = report.buckets.flatMap((bucket) => bucket.rows),
): UsageLabelContext {
  const sourceLabelCounts = new Map<string, number>();
  for (const source of report.sources) {
    const label = sourceLabel(report, source.source_id);
    sourceLabelCounts.set(label, (sourceLabelCounts.get(label) ?? 0) + 1);
  }

  const identityLabelCounts = new Map<string, number>();
  const identities = usageIdentities(report, rows);
  for (const identity of identities) {
    const label = identityLabel(identity);
    identityLabelCounts.set(label, (identityLabelCounts.get(label) ?? 0) + 1);
  }
  const sourceQualifiedIdentityCounts = new Map<string, number>();
  for (const identity of identities) {
    const label = identityLabel(identity);
    if (identityLabelCounts.get(label) === 1) continue;
    const qualifiedLabel = `${label} · ${identity.sourceId}`;
    sourceQualifiedIdentityCounts.set(qualifiedLabel, (sourceQualifiedIdentityCounts.get(qualifiedLabel) ?? 0) + 1);
  }

  const identityCollisionLabels = new Set(
    [...identityLabelCounts.entries()]
      .filter(([, count]) => count > 1)
      .map(([label]) => label),
  );
  return {
    sourceCollisionLabels: new Set(
      [...sourceLabelCounts.entries()]
        .filter(([, count]) => count > 1)
        .map(([label]) => label),
    ),
    identityCollisionLabels,
    identityModelCollisionKeys: new Set(
      identities
        .filter((identity) => {
          const label = identityLabel(identity);
          return identityCollisionLabels.has(label)
            && (sourceQualifiedIdentityCounts.get(`${label} · ${identity.sourceId}`) ?? 0) > 1;
        })
        .map((identity) => identity.key),
    ),
  };
}

export function sourceIdentityLabel(
  report: UsageReport,
  sourceId: string,
  context = usageLabelContext(report),
): string {
  const label = sourceLabel(report, sourceId);
  return context.sourceCollisionLabels.has(label) ? `${label} · ${sourceId}` : label;
}

export function identityDisplayLabel(
  identity: UsageIdentity,
  locale = 'en',
  context?: UsageLabelContext,
): string {
  const label = identityLabel(identity, locale);
  if (!context?.identityCollisionLabels.has(identityLabel(identity))) return label;
  const modelSuffix = context.identityModelCollisionKeys.has(identity.key) ? ` · ${identity.modelId}` : '';
  return `${label} · ${identity.sourceId}${modelSuffix}`;
}

export function filterBucketRows(bucket: UsageBucket, filter: UsageFilter): UsageBucketRow[] {
  return bucket.rows.filter((row) => (
    (filter.sourceIds.length === 0 || filter.sourceIds.includes(row.source_id))
    && (filter.modelKeys.length === 0 || filter.modelKeys.includes(pairKey(row.source_id, row.model_id)))
  ));
}

export function filteredRows(report: UsageReport, filter: UsageFilter): UsageBucketRow[] {
  return report.buckets.flatMap((bucket) => filterBucketRows(bucket, filter));
}

const typeSeries = (
  rows: UsageBucketRow[],
  metric: UsageMetric,
  historyComplete: boolean,
): Array<[string, number | null]> => {
  if (!historyComplete && rows.length === 0) {
    if (metric === 'requests') return [['requests', null]];
    if (metric === 'output') return [['output', null]];
    if (metric === 'cache') return [['cache', null]];
    if (metric === 'input') return [['input', null], ['cache', null]];
    return [['input', null], ['cache', null], ['output', null]];
  }
  const counters = aggregateCounters(rows);
  if (metric === 'requests') return [['requests', counters.requests]];
  if (!usageTokensAreKnown(counters)) return metric === 'output'
    ? [['output', null]]
    : metric === 'cache'
      ? [['cache', null]]
      : metric === 'input'
        ? [['input', null], ['cache', null]]
        : [['input', null], ['cache', null], ['output', null]];
  if (metric === 'output') return [['output', counters.output_tokens]];
  if (metric === 'cache') return [['cache', counters.cached_input_tokens]];
  if (metric === 'input') return [['input', usageNonCachedInput(counters)], ['cache', counters.cached_input_tokens]];
  return [
    ['input', usageNonCachedInput(counters)],
    ['cache', counters.cached_input_tokens],
    ['output', counters.output_tokens],
  ];
};

const bucketMetricValue = (
  bucket: UsageBucket,
  rows: UsageBucketRow[],
  metric: UsageMetric,
): number | null => {
  if (!bucket.history_complete && rows.length === 0) return null;
  return usageMetricValue(aggregateCounters(rows), metric);
};

export function seriesFor(
  report: UsageReport,
  filter: UsageFilter,
  group: UsageGroup,
  metric: UsageMetric,
  locale = 'en',
): UsageSeries[] {
  const rows = filteredRows(report, filter);
  if (group === 'type') {
    const valuesByKey = new Map<string, Array<number | null>>();
    for (const bucket of report.buckets) {
      for (const [key, value] of typeSeries(filterBucketRows(bucket, filter), metric, bucket.history_complete)) {
        valuesByKey.set(key, [...(valuesByKey.get(key) ?? []), value]);
      }
    }
    const labels: Record<string, string> = {
      input: 'Input (non-cache)',
      cache: 'Cache reads',
      output: 'Output',
      requests: 'Requests',
    };
    return [...valuesByKey.entries()].map(([key, values], index) => ({
      key,
      label: labels[key],
      colorIndex: index,
      values,
    }));
  }

  if (group === 'total') {
    return [{
      key: 'total',
      label: metric,
      colorIndex: 0,
      values: report.buckets.map((bucket) => bucketMetricValue(
        bucket,
        filterBucketRows(bucket, filter),
        metric,
      )),
    }];
  }

  if (group === 'source') {
    const labelContext = usageLabelContext(report);
    const sourceIds = [...new Set(rows.map((row) => row.source_id))];
    return sourceIds.map((sourceId, index) => ({
      key: sourceId,
      label: sourceIdentityLabel(report, sourceId, labelContext),
      colorIndex: index,
      values: report.buckets.map((bucket) => {
        const rowsForSource = filterBucketRows(bucket, filter).filter((row) => row.source_id === sourceId);
        return bucketMetricValue(bucket, rowsForSource, metric);
      }),
    }));
  }

  const identities = usageIdentities(report, rows);
  const labelContext = usageLabelContext(report);
  return identities.map((identity, index) => ({
    key: identity.key,
    label: identityDisplayLabel(identity, locale, labelContext),
    colorIndex: index,
    values: report.buckets.map((bucket) => {
      const rowsForIdentity = filterBucketRows(bucket, filter).filter((row) => (
        row.source_id === identity.sourceId && row.model_id === identity.modelId
      ));
      return bucketMetricValue(bucket, rowsForIdentity, metric);
    }),
  }));
}

export function reportHasPartialHistory(report: UsageReport, filter: UsageFilter): boolean {
  void filter;
  return report.buckets.some((bucket) => !bucket.history_complete);
}

export function reportHasUnknownTokens(report: UsageReport, filter: UsageFilter): boolean {
  return report.buckets.some((bucket) => {
    const rows = filterBucketRows(bucket, filter);
    const counters = aggregateCounters(rows);
    return rows.length > 0 && !usageTokensAreKnown(counters);
  });
}
