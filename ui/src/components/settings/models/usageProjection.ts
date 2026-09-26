import type {
  UsageBucket,
  UsageBucketRow,
  UsageCounters,
  UsageReport,
  UsageSummary,
  UsageWindowKey,
} from './types';

export const USAGE_WINDOW_OPTIONS = ['24h', '7d', '30d', '60d'] as const satisfies readonly UsageWindowKey[];
export type UsageWindowOption = UsageWindowKey;

export type UsageMetric = 'tokens' | 'input' | 'output' | 'cache' | 'requests' | 'cost';
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
  sourceDisplayLabels: ReadonlyMap<string, string>;
  identityCollisionLabels: ReadonlySet<string>;
  identityModelCollisionKeys: ReadonlySet<string>;
  identityDisplayLabels: ReadonlyMap<string, string>;
};

export type UsageSeries = {
  key: string;
  label: string;
  colorIndex: number;
  values: Array<number | null>;
  /** Per bucket, whether the value is only a floor (`api_cost_lower_bound`); cost only. */
  floors: boolean[];
  /** Per bucket, whether nothing in the value is priced, so it reads as no price, never $0; cost only. */
  unpriced: boolean[];
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
  const total = rows.reduce<UsageCounters>((sum, row) => ({
    requests: sum.requests + row.requests,
    token_reports: sum.token_reports + row.token_reports,
    input_tokens: sum.input_tokens + row.input_tokens,
    cached_input_tokens: sum.cached_input_tokens + row.cached_input_tokens,
    output_tokens: sum.output_tokens + row.output_tokens,
  }), emptyCounters());
  // A priced row carries its cost; the sum is priced only when every row is.
  if (rows.length > 0 && rows.every(usageIsPriced)) {
    total.api_cost_usd = rows.reduce((sum, row) => sum + (row.api_cost_usd ?? 0), 0);
    total.excluded_tokens = rows.reduce((sum, row) => sum + (row.excluded_tokens ?? 0), 0);
    total.api_cost_lower_bound = rows.some((row) => row.api_cost_lower_bound === true);
  }
  return total;
}

/** Whether the server priced these counters at API prices. */
export const usageIsPriced = (counters: UsageCounters): boolean => typeof counters.api_cost_usd === 'number';

/** Every priced token of these counters belongs to a model with no known price. */
export const usageHasNoPrice = (counters: UsageCounters): boolean =>
  usageIsPriced(counters) && (counters.excluded_tokens ?? 0) > 0 && (counters.api_cost_usd ?? 0) === 0;

/** Whether the report carries API-price value at all: a server that predates it never does. */
export const reportIsPriced = (report: UsageSummary): boolean => report.pricing !== undefined;

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
  // Nothing metered costs nothing, priced or not.
  if (metric === 'cost') return usageIsPriced(counters) ? counters.api_cost_usd ?? 0 : counters.requests === 0 ? 0 : null;
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

type LocalStamp = { year: number; month: number; day: number; hour: string; minute: string };

const localStamp = (value: string): LocalStamp | null => {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!match) return null;
  const [, year, month, day, hour, minute] = match;
  return { year: Number(year), month: Number(month), day: Number(day), hour, minute };
};

const offsetMinutes = (value: string): number | null => {
  const match = value.match(/(Z|([+-])(\d{2}):(\d{2}))$/);
  if (!match) return null;
  if (match[1] === 'Z') return 0;
  return (match[2] === '-' ? -1 : 1) * (Number(match[3]) * 60 + Number(match[4]));
};

/**
 * A bucket's range as a tooltip heading: 「9月23日 22:00–23:00」, one date when the
 * range stays on one day, and only the date for a whole day. The zone is set
 * apart so the heading can mute it; it is named only when the two ends differ
 * (a DST change) or the report's offset is not the viewer's own.
 */
export function formatBucketHeading(bucket: UsageBucket, locale: string): { range: string; zone: string | null } {
  const start = localStamp(bucket.start_at);
  const end = localStamp(bucket.end_at);
  if (!start || !end) return { range: formatBucketRange(bucket, locale, true), zone: null };
  const date = ({ year, month, day }: LocalStamp) => (locale.startsWith('zh')
    ? `${month}月${day}日`
    : new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', timeZone: 'UTC' })
      .format(new Date(Date.UTC(year, month - 1, day))));
  const separator = locale.startsWith('zh') ? ' ' : ', ';
  const time = (stamp: LocalStamp) => `${stamp.hour}:${stamp.minute}`;
  const dayOf = (stamp: LocalStamp) => Date.UTC(stamp.year, stamp.month - 1, stamp.day);
  const endsAtMidnight = end.hour === '00' && end.minute === '00';
  const nextDay = dayOf(end) - dayOf(start) === 24 * 60 * 60 * 1000;
  let range: string;
  if (dayOf(start) === dayOf(end)) {
    range = `${date(start)}${separator}${time(start)}–${time(end)}`;
  } else if (nextDay && endsAtMidnight) {
    range = time(start) === '00:00' ? date(start) : `${date(start)}${separator}${time(start)}–24:00`;
  } else {
    range = `${date(start)}${separator}${time(start)} – ${date(end)}${separator}${time(end)}`;
  }
  const startZone = formatOffset(bucket.start_at);
  const endZone = formatOffset(bucket.end_at);
  const reportOffset = offsetMinutes(bucket.start_at);
  const hostOffset = -new Date(bucket.start_at).getTimezoneOffset();
  const zone = startZone !== endZone
    ? `${startZone} – ${endZone}`
    : reportOffset !== null && reportOffset !== hostOffset ? startZone : null;
  return { range, zone };
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

export function identityLabel(identity: UsageIdentity, unknownModelLabel: string): string {
  const model = identity.modelLabel || unknownModelLabel;
  return `${identity.sourceLabel} · ${model}`;
}

export function usageLabelContext(
  report: UsageReport,
  rows: readonly UsageBucketRow[],
  unknownModelLabel: string,
): UsageLabelContext {
  const sourceIds = [...new Set([
    ...report.sources.map((source) => source.source_id),
    ...rows.map((row) => row.source_id),
  ])];
  const sourceLabels = new Map(
    sourceIds.map((sourceId) => [sourceId, sourceLabel(report, sourceId)] as const),
  );
  const sourceLabelCounts = new Map<string, number>();
  for (const label of sourceLabels.values()) {
    sourceLabelCounts.set(label, (sourceLabelCounts.get(label) ?? 0) + 1);
  }
  const sourceCollisionLabels = new Set(
    [...sourceLabelCounts.entries()]
      .filter(([, count]) => count > 1)
      .map(([label]) => label),
  );
  const sourceDisplayLabels = new Map(
    [...sourceLabels.entries()].map(([sourceId, label]) => [
      sourceId,
      sourceCollisionLabels.has(label) ? `${label} · ${sourceId}` : label,
    ]),
  );
  for (;;) {
    const displayCounts = new Map<string, number>();
    for (const label of sourceDisplayLabels.values()) {
      displayCounts.set(label, (displayCounts.get(label) ?? 0) + 1);
    }
    const collisions = [...displayCounts.entries()]
      .filter(([, count]) => count > 1)
      .map(([label]) => label);
    if (collisions.length === 0) break;
    for (const [sourceId, label] of sourceDisplayLabels) {
      if (collisions.includes(label)) {
        sourceDisplayLabels.set(sourceId, `${label} · ${sourceId}`);
      }
    }
  }

  const identityLabelCounts = new Map<string, number>();
  const identities = usageIdentities(report, rows);
  for (const identity of identities) {
    const label = identityLabel(identity, unknownModelLabel);
    identityLabelCounts.set(label, (identityLabelCounts.get(label) ?? 0) + 1);
  }
  const sourceQualifiedIdentityCounts = new Map<string, number>();
  for (const identity of identities) {
    const label = identityLabel(identity, unknownModelLabel);
    if (identityLabelCounts.get(label) === 1) continue;
    const qualifiedLabel = `${label} · ${identity.sourceId}`;
    sourceQualifiedIdentityCounts.set(qualifiedLabel, (sourceQualifiedIdentityCounts.get(qualifiedLabel) ?? 0) + 1);
  }

  const identityCollisionLabels = new Set(
    [...identityLabelCounts.entries()]
      .filter(([, count]) => count > 1)
      .map(([label]) => label),
  );
  const identityByKey = new Map(identities.map((identity) => [identity.key, identity] as const));
  const identityCandidates = new Map(
    identities.map((identity) => {
      const label = identityLabel(identity, unknownModelLabel);
      const modelSuffix = (
        (sourceQualifiedIdentityCounts.get(`${label} · ${identity.sourceId}`) ?? 0) > 1
      ) ? ` · ${identity.modelId}` : '';
      const candidate = identityCollisionLabels.has(label)
        ? `${label} · ${identity.sourceId}${modelSuffix}`
        : label;
      return [identity.key, candidate] as const;
    }),
  );
  const identityDisplayLabels = new Map<string, string>();
  const usedIdentityLabels = new Set<string>();
  for (const [key, candidate] of identityCandidates) {
    const identity = identityByKey.get(key)!;
    let displayLabel = candidate;
    let suffix = 0;
    while (usedIdentityLabels.has(displayLabel)) {
      suffix += 1;
      const identitySuffix = ` · ${identity.sourceId} · ${identity.modelId}`;
      displayLabel = `${candidate}${identitySuffix}${suffix > 1 ? ` (${suffix})` : ''}`;
    }
    usedIdentityLabels.add(displayLabel);
    identityDisplayLabels.set(key, displayLabel);
  }
  return {
    sourceCollisionLabels,
    sourceDisplayLabels,
    identityCollisionLabels,
    identityModelCollisionKeys: new Set(
      identities
        .filter((identity) => {
          const label = identityLabel(identity, unknownModelLabel);
          return identityCollisionLabels.has(label)
            && (sourceQualifiedIdentityCounts.get(`${label} · ${identity.sourceId}`) ?? 0) > 1;
        })
        .map((identity) => identity.key),
    ),
    identityDisplayLabels,
  };
}

export function sourceIdentityLabel(
  report: UsageReport,
  sourceId: string,
  context: UsageLabelContext,
): string {
  const label = sourceLabel(report, sourceId);
  return context.sourceDisplayLabels.get(sourceId)
    ?? (context.sourceCollisionLabels.has(label) ? `${label} · ${sourceId}` : label);
}

export function identityDisplayLabel(
  identity: UsageIdentity,
  unknownModelLabel: string,
  context?: UsageLabelContext,
): string {
  const displayLabel = context?.identityDisplayLabels.get(identity.key);
  if (displayLabel) return displayLabel;
  const label = identityLabel(identity, unknownModelLabel);
  if (!context?.identityCollisionLabels.has(label)) return label;
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
  if (metric === 'cost') {
    return [['cost', !historyComplete && rows.length === 0 ? null : usageMetricValue(aggregateCounters(rows), 'cost')]];
  }
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

/** Whether these rows' cost is only a floor, as the server marked it. */
export const costIsFloor = (rows: readonly UsageCounters[], metric: UsageMetric): boolean => (
  metric === 'cost' && aggregateCounters(rows).api_cost_lower_bound === true
);

/** Whether these rows' cost has nothing priced in it, as opposed to a floor of some priced part. */
export const costIsUnpriced = (rows: readonly UsageCounters[], metric: UsageMetric): boolean => (
  metric === 'cost' && usageHasNoPrice(aggregateCounters(rows))
);

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
  unknownModelLabel: string,
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
      cost: 'API price',
    };
    const floors = report.buckets.map((bucket) => costIsFloor(filterBucketRows(bucket, filter), metric));
    const unpriced = report.buckets.map((bucket) => costIsUnpriced(filterBucketRows(bucket, filter), metric));
    return [...valuesByKey.entries()].map(([key, values], index) => ({
      key,
      label: labels[key],
      colorIndex: index,
      values,
      floors,
      unpriced,
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
      floors: report.buckets.map((bucket) => costIsFloor(filterBucketRows(bucket, filter), metric)),
      unpriced: report.buckets.map((bucket) => costIsUnpriced(filterBucketRows(bucket, filter), metric)),
    }];
  }

  if (group === 'source') {
    const labelContext = usageLabelContext(report, rows, unknownModelLabel);
    const sourceIds = [...new Set(rows.map((row) => row.source_id))];
    return sourceIds.map((sourceId, index) => ({
      key: sourceId,
      label: sourceIdentityLabel(report, sourceId, labelContext),
      colorIndex: index,
      values: report.buckets.map((bucket) => {
        const rowsForSource = filterBucketRows(bucket, filter).filter((row) => row.source_id === sourceId);
        return bucketMetricValue(bucket, rowsForSource, metric);
      }),
      floors: report.buckets.map((bucket) => costIsFloor(
        filterBucketRows(bucket, filter).filter((row) => row.source_id === sourceId),
        metric,
      )),
      unpriced: report.buckets.map((bucket) => costIsUnpriced(
        filterBucketRows(bucket, filter).filter((row) => row.source_id === sourceId),
        metric,
      )),
    }));
  }

  const identities = usageIdentities(report, rows);
  const labelContext = usageLabelContext(report, rows, unknownModelLabel);
  return identities.map((identity, index) => ({
    key: identity.key,
    label: identityDisplayLabel(identity, unknownModelLabel, labelContext),
    colorIndex: index,
    values: report.buckets.map((bucket) => {
      const rowsForIdentity = filterBucketRows(bucket, filter).filter((row) => (
        row.source_id === identity.sourceId && row.model_id === identity.modelId
      ));
      return bucketMetricValue(bucket, rowsForIdentity, metric);
    }),
    floors: report.buckets.map((bucket) => costIsFloor(
      filterBucketRows(bucket, filter).filter((row) => (
        row.source_id === identity.sourceId && row.model_id === identity.modelId
      )),
      metric,
    )),
    unpriced: report.buckets.map((bucket) => costIsUnpriced(
      filterBucketRows(bucket, filter).filter((row) => (
        row.source_id === identity.sourceId && row.model_id === identity.modelId
      )),
      metric,
    )),
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
