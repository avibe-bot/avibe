import type {
  ServiceCounters,
  UsageBucket,
  UsageBucketEntry,
  UsageBucketRow,
  UsageCounters,
  UsageReport,
  UsageServiceBucketRow,
  UsageSummary,
  UsageTotals,
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

export type UsageSeries = {
  key: string;
  label: string;
  colorIndex: number;
  values: Array<number | null>;
  /** Per bucket, whether the value is only a floor (`api_cost_lower_bound`); cost only. */
  floors: boolean[];
  /** Per bucket, whether nothing in the value is priced, so it reads as no price, never $0; cost only. */
  unpriced: boolean[];
  /** The removed-Sources aggregate, drawn in a neutral colour rather than a palette one. */
  removed?: boolean;
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

/** Model counters, or a search-service row: what a usage total can be summed from. */
type CountedRow = UsageCounters | UsageServiceBucketRow;

export const isServiceRow = (row: CountedRow): row is UsageServiceBucketRow =>
  (row as { kind?: unknown }).kind === 'service';

export const isModelRow = (row: UsageBucketEntry): row is UsageBucketRow => !isServiceRow(row);

/** A search service's totals, and its billable requests the current catalog no longer prices. */
export type ServiceTotals = ServiceCounters & { excluded_service_requests: number };

/**
 * The (Source, service) pairs whose service the current catalog no longer
 * prices. A bucket row cannot say so; `services` does, for the whole window.
 */
export const unpricedServices = (report: UsageSummary): ReadonlySet<string> => new Set(report.services
  .filter((service) => service.priced === false)
  .map((service) => pairKey(service.source_id, service.service_id)));

export function aggregateServiceRows(
  rows: readonly UsageServiceBucketRow[],
  unpriced: ReadonlySet<string>,
): ServiceTotals {
  const sum = (value: (row: UsageServiceBucketRow) => number) => rows.reduce((total, row) => total + value(row), 0);
  const total: ServiceTotals = {
    requests: sum((row) => row.requests),
    billable_requests: sum((row) => row.billable_requests),
    uncertain_requests: sum((row) => row.uncertain_requests),
    reported_requests: sum((row) => row.reported_requests),
    excluded_service_requests: sum((row) => (unpriced.has(pairKey(row.source_id, row.service_id)) ? row.billable_requests : 0)),
  };
  if (rows.length > 0 && rows.every(usageIsPriced)) {
    total.api_cost_usd = sum((row) => row.api_cost_usd ?? 0);
    total.reported_usd = sum((row) => row.reported_usd ?? 0);
    total.api_cost_lower_bound = rows.some((row) => row.api_cost_lower_bound === true);
  }
  return total;
}

/**
 * A search request is not a model call, so a service row adds no request or
 * token to the sum, only its cost. The billable requests of a service the
 * catalog no longer prices (`unpricedServices`) are left out of that cost, as
 * an unpriced model's tokens are.
 */
export function aggregateCounters(rows: readonly CountedRow[], unpriced: ReadonlySet<string> = new Set()): UsageTotals {
  const models = rows.filter((row): row is UsageCounters => !isServiceRow(row));
  const total = models.reduce<UsageTotals>((sum, row) => ({
    requests: sum.requests + row.requests,
    token_reports: sum.token_reports + row.token_reports,
    input_tokens: sum.input_tokens + row.input_tokens,
    cached_input_tokens: sum.cached_input_tokens + row.cached_input_tokens,
    output_tokens: sum.output_tokens + row.output_tokens,
  }), emptyCounters());
  // A priced row carries its cost; the sum is priced only when every row is.
  if (rows.length > 0 && rows.every(usageIsPriced)) {
    const services = aggregateServiceRows(rows.filter(isServiceRow), unpriced);
    total.api_cost_usd = rows.reduce((sum, row) => sum + (row.api_cost_usd ?? 0), 0);
    total.excluded_tokens = models.reduce((sum, row) => sum + (row.excluded_tokens ?? 0), 0);
    total.api_cost_lower_bound = rows.some((row) => row.api_cost_lower_bound === true);
    total.service_reported_usd = services.reported_usd ?? 0;
    total.excluded_service_requests = services.excluded_service_requests;
  }
  return total;
}

/** Whether the server priced these counters at API prices. */
export const usageIsPriced = (counters: { api_cost_usd?: number }): boolean => typeof counters.api_cost_usd === 'number';

/** Everything these counters would price is left out: models and services with no known price. */
export const usageHasNoPrice = (
  counters: { api_cost_usd?: number; excluded_tokens?: number; excluded_service_requests?: number },
): boolean => usageIsPriced(counters) && (counters.api_cost_usd ?? 0) === 0
  && ((counters.excluded_tokens ?? 0) > 0 || (counters.excluded_service_requests ?? 0) > 0);

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

/**
 * Every Source config no longer holds, as one identity. It sorts before any
 * Source ID or pair key, and neither can spell it: a pair key never starts with
 * its separator.
 */
export const REMOVED_SOURCES_KEY = `${PAIR_SEPARATOR}removed`;

/** Grouped by model, every live Source's search services as one identity: none of them is a model. */
export const SEARCH_SERVICES_KEY = `${PAIR_SEPARATOR}services`;

/**
 * The longest ledger key, in code points as the ledger counts them, that is
 * still the identifier itself; a longer one is a head plus a digest.
 */
const LEDGER_VERBATIM_MAX_LENGTH = 200;

export type UsageText = {
  unknownModel: string;
  removedSources: string;
  searchServices: string;
};

export type UsageIdentity = {
  key: string;
  label: string;
  /** Beside a model, the Source it was metered under; empty for a Source and for the removed aggregate. */
  sourceLabel: string;
  /** The aggregate of every Source config no longer holds. */
  removed: boolean;
  /** A model its live Source does not list, named by the ID it was metered under. */
  unlisted: boolean;
};

export type UsageIdentities = {
  keyOf: (row: UsageBucketEntry) => string;
  get: (key: string) => UsageIdentity;
  /** These rows' identities in first-metered order, the removed aggregate last. */
  of: (rows: readonly UsageBucketEntry[]) => UsageIdentity[];
};

const allRows = (report: UsageReport): UsageBucketEntry[] => report.buckets.flatMap((bucket) => bucket.rows);

/**
 * The Sources config still holds, by ID. A configured Source always carries its
 * name, so the report leaves the label null exactly when config let it go. A
 * tool-only Source is named only by its search usage.
 */
const liveSources = (report: UsageReport) => new Map([...report.sources, ...report.services]
  .filter((source) => source.label != null)
  .map((source) => [source.source_id, source.label!.trim()] as const));

/** The model label config holds for this pair, or null when the Source does not list the model. */
export function modelLabel(report: UsageReport, sourceId: string, modelId: string): string | null {
  const source = report.sources.find((candidate) => candidate.source_id === sourceId);
  const model = source?.models.find((candidate) => candidate.model_id === modelId);
  return model?.label?.trim() || null;
}

/**
 * The name a model reads by: its configured label, else the model ID it was
 * metered under, which the ledger key is. Only a folded key, a head plus a
 * digest, has no name left to show.
 */
export function usageModelName(report: UsageReport, sourceId: string, modelId: string, unknownModel: string): string {
  const label = modelLabel(report, sourceId, modelId);
  if (label) return label;
  return !modelId.trim() || [...modelId].length > LEDGER_VERBATIM_MAX_LENGTH ? unknownModel : modelId;
}

const byKey = <T extends readonly [string, ...unknown[]]>([left]: T, [right]: T) => (left < right ? -1 : left > right ? 1 : 0);

/**
 * Keep labels that name different identities apart without printing an ID:
 * the first holder of a label keeps it, and each later one, in the order
 * given, reads 「label (2)」, 「label (3)」 …
 */
function distinctLabels(entries: ReadonlyArray<readonly [string, string]>): Map<string, string> {
  const counts = new Map<string, number>();
  for (const [, label] of entries) counts.set(label, (counts.get(label) ?? 0) + 1);
  const used = new Set(entries.filter(([, label]) => counts.get(label) === 1).map(([, label]) => label));
  const labels = new Map<string, string>();
  for (const [key, label] of entries) {
    if (counts.get(label) === 1) {
      labels.set(key, label);
      continue;
    }
    let candidate = label;
    for (let ordinal = 2; used.has(candidate); ordinal += 1) candidate = `${label} (${ordinal})`;
    used.add(candidate);
    labels.set(key, candidate);
  }
  return labels;
}

/**
 * The identities a report's rows are shown under. A live Source, or a model
 * under one, keeps its own identity; every row of a Source config no longer
 * holds folds into one removed aggregate, so its counters still add up while
 * no Source ID reaches the screen. Labels come from the whole report, so a
 * filter never renames what stays on screen. A search-service row belongs to
 * its Source, and grouped by model, to the one search-services identity.
 */
export function usageIdentities(report: UsageReport, group: 'model' | 'source', text: UsageText): UsageIdentities {
  const live = liveSources(report);
  const removed = (sourceId: string) => !live.has(sourceId);
  const keyOf = (row: UsageBucketEntry) => (removed(row.source_id)
    ? REMOVED_SOURCES_KEY
    : group === 'source' ? row.source_id : isServiceRow(row) ? SEARCH_SERVICES_KEY : pairKey(row.source_id, row.model_id));
  const rows = allRows(report);
  const hasRemoved = [...report.sources, ...report.services, ...rows].some((entry) => removed(entry.source_id));
  const removedEntry = hasRemoved ? [[REMOVED_SOURCES_KEY, text.removedSources] as const] : [];
  // The aggregate keeps the product's own name; live Sources tie-break by ID.
  const sourceLabels = distinctLabels([...removedEntry, ...[...live].sort(byKey)]);
  const identities = new Map<string, UsageIdentity>();
  if (hasRemoved) {
    identities.set(REMOVED_SOURCES_KEY, {
      key: REMOVED_SOURCES_KEY,
      label: text.removedSources,
      sourceLabel: '',
      removed: true,
      unlisted: false,
    });
  }
  if (group === 'source') {
    for (const sourceId of live.keys()) {
      identities.set(sourceId, {
        key: sourceId,
        label: sourceLabels.get(sourceId) ?? '',
        sourceLabel: '',
        removed: false,
        unlisted: false,
      });
    }
  } else {
    const pairs = new Map(rows
      .filter(isModelRow)
      .filter((row) => !removed(row.source_id))
      .map((row) => [pairKey(row.source_id, row.model_id), row] as const));
    const models = new Map([...pairs].map(([key, row]) => [key, {
      sourceLabel: sourceLabels.get(row.source_id) ?? '',
      name: usageModelName(report, row.source_id, row.model_id, text.unknownModel),
      unlisted: modelLabel(report, row.source_id, row.model_id) === null,
    }] as const));
    // A configured model keeps its name over one that only reads like it.
    const labels = distinctLabels([
      ...removedEntry,
      ...[...models]
        .sort((left, right) => Number(left[1].unlisted) - Number(right[1].unlisted) || byKey(left, right))
        .map(([key, model]) => [key, `${model.sourceLabel} · ${model.name}`] as const),
    ]);
    for (const [key, model] of models) {
      identities.set(key, {
        key,
        label: labels.get(key) ?? '',
        sourceLabel: model.sourceLabel,
        removed: false,
        unlisted: model.unlisted,
      });
    }
    if (rows.some((row) => isServiceRow(row) && !removed(row.source_id))) {
      identities.set(SEARCH_SERVICES_KEY, {
        key: SEARCH_SERVICES_KEY,
        label: text.searchServices,
        sourceLabel: '',
        removed: false,
        unlisted: false,
      });
    }
  }
  const get = (key: string) => identities.get(key)!;
  return {
    keyOf,
    get,
    of: (selected) => {
      const keys = [...new Set(selected.map(keyOf))];
      return [
        ...keys.filter((key) => key !== REMOVED_SOURCES_KEY),
        ...keys.filter((key) => key === REMOVED_SOURCES_KEY),
      ].map(get);
    },
  };
}

/**
 * A selection is kept across reports, so it can name identities the current
 * one no longer offers. A Source config let go since it was picked, or any
 * pair of it, now reads as the removed aggregate. Any other key this report has
 * no option for is dropped, the aggregate itself once nothing folds into it, so
 * the filters never hold a choice they cannot show.
 */
export function foldUsageSelection(report: UsageReport, selection: UsageFilter): UsageFilter {
  const live = liveSources(report);
  const rows = allRows(report);
  const known = new Set([...report.sources, ...report.services, ...rows].map((entry) => entry.source_id));
  const fold = (sourceId: string, key: string) => (live.has(sourceId) ? key : REMOVED_SOURCES_KEY);
  // What each filter lists: every Source the report names, and every model pair it metered.
  const offered = {
    sourceIds: new Set([...known].map((sourceId) => fold(sourceId, sourceId))),
    modelKeys: new Set(rows.filter(isModelRow).map((row) => fold(row.source_id, pairKey(row.source_id, row.model_id)))),
  };
  const reconcile = (keys: readonly string[], options: ReadonlySet<string>, sourceOf: (key: string) => string) => [
    ...new Set(keys.map((key) => (known.has(sourceOf(key)) ? fold(sourceOf(key), key) : key))),
  ].filter((key) => options.has(key));
  return {
    sourceIds: reconcile(selection.sourceIds, offered.sourceIds, (key) => key),
    modelKeys: reconcile(
      selection.modelKeys,
      offered.modelKeys,
      (key) => key.slice(0, Math.max(0, key.indexOf(PAIR_SEPARATOR))),
    ),
  };
}

/**
 * A selection names identities; the rows it keeps are named by the IDs they
 * were metered under. The removed aggregate therefore stands for every Source
 * ID, and every pair, it folded. It stays in the list too, matching no row, so
 * picking it never widens to everything while it is still offered.
 */
export function resolveUsageFilter(report: UsageReport, picked: UsageFilter): UsageFilter {
  const selection = foldUsageSelection(report, picked);
  if (!selection.sourceIds.includes(REMOVED_SOURCES_KEY) && !selection.modelKeys.includes(REMOVED_SOURCES_KEY)) {
    return selection;
  }
  const live = liveSources(report);
  const removedRows = allRows(report).filter((row) => !live.has(row.source_id));
  const expand = (keys: readonly string[], folded: string[]) => [...new Set(keys.flatMap((key) => (
    key === REMOVED_SOURCES_KEY ? [key, ...folded] : [key]
  )))];
  return {
    sourceIds: expand(selection.sourceIds, removedRows.map((row) => row.source_id)),
    modelKeys: expand(selection.modelKeys, removedRows.filter(isModelRow).map((row) => pairKey(row.source_id, row.model_id))),
  };
}

/** A search request is no model's, so a model filter leaves out every service row; a Source filter keeps its own. */
export function filterBucketRows(bucket: UsageBucket, filter: UsageFilter): UsageBucketEntry[] {
  return bucket.rows.filter((row) => (
    (filter.sourceIds.length === 0 || filter.sourceIds.includes(row.source_id))
    && (filter.modelKeys.length === 0 || (isModelRow(row) && filter.modelKeys.includes(pairKey(row.source_id, row.model_id))))
  ));
}

export function filteredRows(report: UsageReport, filter: UsageFilter): UsageBucketEntry[] {
  return report.buckets.flatMap((bucket) => filterBucketRows(bucket, filter));
}

/** The rows a metric counts: a service row has a cost, but no call or token. */
export const metricRows = (rows: readonly UsageBucketEntry[], metric: UsageMetric): UsageBucketEntry[] => (
  metric === 'cost' ? [...rows] : rows.filter(isModelRow)
);

const typeSeries = (
  rows: UsageBucketEntry[],
  metric: UsageMetric,
  historyComplete: boolean,
  unpriced: ReadonlySet<string>,
): Array<[string, number | null]> => {
  if (metric === 'cost') {
    return [['cost', !historyComplete && rows.length === 0 ? null : usageMetricValue(aggregateCounters(rows, unpriced), 'cost')]];
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
export const costIsFloor = (rows: readonly CountedRow[], metric: UsageMetric): boolean => (
  metric === 'cost' && aggregateCounters(rows).api_cost_lower_bound === true
);

/** Whether these rows' cost has nothing priced in it, as opposed to a floor of some priced part. */
export const costIsUnpriced = (rows: readonly CountedRow[], metric: UsageMetric, unpriced: ReadonlySet<string>): boolean => (
  metric === 'cost' && usageHasNoPrice(aggregateCounters(rows, unpriced))
);

const bucketMetricValue = (
  bucket: UsageBucket,
  rows: UsageBucketEntry[],
  metric: UsageMetric,
  unpriced: ReadonlySet<string>,
): number | null => {
  if (!bucket.history_complete && rows.length === 0) return null;
  return usageMetricValue(aggregateCounters(rows, unpriced), metric);
};

export function seriesFor(
  report: UsageReport,
  filter: UsageFilter,
  group: UsageGroup,
  metric: UsageMetric,
  text: UsageText,
): UsageSeries[] {
  const inScope = (bucket: UsageBucket) => metricRows(filterBucketRows(bucket, filter), metric);
  const unpricedKeys = unpricedServices(report);
  if (group === 'type') {
    const valuesByKey = new Map<string, Array<number | null>>();
    for (const bucket of report.buckets) {
      for (const [key, value] of typeSeries(inScope(bucket), metric, bucket.history_complete, unpricedKeys)) {
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
    const floors = report.buckets.map((bucket) => costIsFloor(inScope(bucket), metric));
    const unpriced = report.buckets.map((bucket) => costIsUnpriced(inScope(bucket), metric, unpricedKeys));
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
      values: report.buckets.map((bucket) => bucketMetricValue(bucket, inScope(bucket), metric, unpricedKeys)),
      floors: report.buckets.map((bucket) => costIsFloor(inScope(bucket), metric)),
      unpriced: report.buckets.map((bucket) => costIsUnpriced(inScope(bucket), metric, unpricedKeys)),
    }];
  }

  const identities = usageIdentities(report, group, text);
  return identities.of(report.buckets.flatMap(inScope)).map((identity, index) => {
    const rowsOf = (bucket: UsageBucket) => inScope(bucket).filter((row) => identities.keyOf(row) === identity.key);
    return {
      key: identity.key,
      label: identity.label,
      colorIndex: index,
      removed: identity.removed,
      values: report.buckets.map((bucket) => bucketMetricValue(bucket, rowsOf(bucket), metric, unpricedKeys)),
      floors: report.buckets.map((bucket) => costIsFloor(rowsOf(bucket), metric)),
      unpriced: report.buckets.map((bucket) => costIsUnpriced(rowsOf(bucket), metric, unpricedKeys)),
    };
  });
}

export function reportHasPartialHistory(report: UsageReport, filter: UsageFilter): boolean {
  void filter;
  return report.buckets.some((bucket) => !bucket.history_complete);
}

export function reportHasUnknownTokens(report: UsageReport, filter: UsageFilter): boolean {
  return report.buckets.some((bucket) => {
    const rows = filterBucketRows(bucket, filter).filter(isModelRow);
    const counters = aggregateCounters(rows);
    return rows.length > 0 && !usageTokensAreKnown(counters);
  });
}

export type ServiceUsageRow = {
  key: string;
  /** The Source's identity, as the Source filter names it. */
  label: string;
  /** The current catalog's name for the service, else its ID; empty for the removed aggregate. */
  service: string;
  tool?: 'web_fetch';
  removed: boolean;
  totals: ServiceTotals;
};

/**
 * The search-service table: one row per live Source and service, busiest
 * first, and every Source config let go folded into the one removed identity
 * last. It follows the Source filter and pinned bucket as the model table does.
 */
export function serviceUsageRows(
  report: UsageReport,
  filter: UsageFilter,
  pinnedKey: string | null,
  text: UsageText,
): ServiceUsageRow[] {
  const buckets = pinnedKey === null ? report.buckets : report.buckets.filter((bucket) => bucket.key === pinnedKey);
  const sources = usageIdentities(report, 'source', text);
  const unpriced = unpricedServices(report);
  const groups = new Map<string, UsageServiceBucketRow[]>();
  for (const row of buckets.flatMap((bucket) => filterBucketRows(bucket, filter)).filter(isServiceRow)) {
    const source = sources.keyOf(row);
    const baseKey = source === REMOVED_SOURCES_KEY ? source : pairKey(row.source_id, row.service_id);
    const key = row.tool === 'web_fetch' ? `${baseKey}:web_fetch` : baseKey;
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  return [...groups].map(([key, rows]) => {
    const [first] = rows;
    const removed = sources.keyOf(first) === REMOVED_SOURCES_KEY;
    const catalogLabel = report.services
      .find((service) => service.source_id === first.source_id && service.service_id === first.service_id
        && (service.tool ?? 'web_search') === (first.tool ?? 'web_search'))
      ?.service_label?.trim();
    return {
      key,
      label: sources.get(sources.keyOf(first)).label,
      service: removed ? '' : catalogLabel || first.service_id,
      ...(first.tool === 'web_fetch' ? { tool: 'web_fetch' as const } : {}),
      removed,
      totals: aggregateServiceRows(rows, unpriced),
    };
  }).sort((left, right) => Number(left.removed) - Number(right.removed) || right.totals.requests - left.totals.requests);
}

export type ServiceMonth = {
  /** The host's current month, `YYYY-MM`. */
  month: string;
  totals: ServiceTotals;
  /** The report carries API-price value at all. */
  priced: boolean;
  /** A day of the month is missing history, so its counts are only a floor. */
  partial: boolean;
};

/**
 * One Source's search requests this month, from a daily report: the day
 * buckets in the month of `to_day`, the host's local today rather than the
 * browser's. A 60-day report covers every day of any month.
 */
export function serviceMonthUsage(report: UsageReport, sourceId: string, tool: 'web_search' | 'web_fetch' = 'web_search'): ServiceMonth {
  const month = report.to_day.slice(0, 7);
  const buckets = report.buckets.filter((bucket) => bucket.key.slice(0, 7) === month);
  const rows = buckets.flatMap((bucket) => bucket.rows).filter(isServiceRow)
    .filter((row) => row.source_id === sourceId && (row.tool ?? 'web_search') === tool);
  return {
    month,
    totals: aggregateServiceRows(rows, unpricedServices(report)),
    priced: reportIsPriced(report),
    partial: buckets.some((bucket) => !bucket.history_complete),
  };
}
