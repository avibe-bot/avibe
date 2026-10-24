import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ServiceUsage, UsageBucket, UsageBucketEntry, UsageBucketRow, UsageCounters, UsageReport, UsageServiceBucketRow } from './types';
import {
  aggregateCounters,
  filteredRows,
  foldUsageSelection,
  formatBucketAxisLabel,
  formatBucketHeading,
  formatBucketRange,
  isModelRow,
  pairKey,
  REMOVED_SOURCES_KEY,
  reportHasPartialHistory,
  reportIsPriced,
  resolveUsageFilter,
  seriesFor,
  serviceMonthUsage,
  serviceUsageRows,
  usageHasNoPrice,
  usageIdentities,
  usageIsEmpty,
  usageIsPriced,
  usageMetricValue,
  usageNonCachedInput,
  usageTotalTokens,
  type UsageFilter,
} from './usageProjection';

const TEXT = { unknownModel: 'Unknown model', removedSources: 'Removed providers', searchServices: 'Search services' };
const TEXT_ZH = { unknownModel: '未知模型', removedSources: '已移除的供应商', searchServices: '搜索服务' };
const NO_FILTER = { sourceIds: [], modelKeys: [] };

const counters = (over: Partial<UsageCounters> = {}): UsageCounters => ({
  requests: 2,
  token_reports: 2,
  input_tokens: 100,
  cached_input_tokens: 25,
  output_tokens: 40,
  ...over,
});

const reportWith = (buckets: UsageBucket[]): UsageReport => ({
  window_days: 1,
  from_day: '2026-09-24',
  to_day: '2026-09-24',
  totals: counters(),
  sources: [{
    source_id: 'source-a',
    label: 'Supplier',
    last_metered_at: null,
    ...counters(),
    models: [{ model_id: 'model-a', label: 'Model', ...counters() }],
  }],
  services: [],
  days: [],
  window_key: '24h',
  granularity: 'hour',
  from_at: '2026-09-24T00:00:00+08:00',
  to_at: '2026-09-24T02:00:00+08:00',
  buckets,
});

const row = (over: Partial<UsageBucketRow> = {}): UsageBucketRow => ({
  ...counters(),
  source_id: 'source-a',
  model_id: 'model-a',
  ...over,
});

const bucket = (key: string, rows: UsageBucketEntry[], history_complete = true): UsageBucket => ({
  key,
  start_at: `2026-09-24T${key}:00:00+08:00`,
  end_at: `2026-09-24T${String(Number(key) + 1).padStart(2, '0')}:00:00+08:00`,
  history_complete,
  rows,
});

describe('usageProjection', () => {
  it('counts input and output once while exposing the cached subset', () => {
    const value = counters({ input_tokens: 148_230, cached_input_tokens: 96_010, output_tokens: 4_120 });
    expect(usageTotalTokens(value)).toBe(152_350);
    expect(usageNonCachedInput(value)).toBe(52_220);
  });

  it('keeps source/model identity separate even when labels match, without printing an ID', () => {
    const value = reportWith([
      bucket('00', [row()]),
      bucket('01', [row({ source_id: 'source-b' })]),
    ]);
    value.sources.push({
      source_id: 'source-b',
      label: 'Supplier',
      last_metered_at: null,
      ...counters(),
      models: [{ model_id: 'model-a', label: 'Model', ...counters() }],
    });
    const series = seriesFor(value, NO_FILTER, 'model', 'tokens', TEXT);
    expect(series.map((item) => item.key)).toEqual([pairKey('source-a', 'model-a'), pairKey('source-b', 'model-a')]);
    expect(series.map((item) => item.label)).toEqual(['Supplier · Model', 'Supplier (2) · Model']);
    expect(seriesFor(value, NO_FILTER, 'source', 'tokens', TEXT).map((item) => item.label)).toEqual(['Supplier', 'Supplier (2)']);
  });

  it('names a model its live Source no longer lists by the model ID it was metered under', () => {
    const value = reportWith([bucket('00', [row({ model_id: 'glm-5.3' })])]);
    expect(usageIdentities(value, 'model', TEXT_ZH).get(pairKey('source-a', 'glm-5.3')))
      .toMatchObject({ label: 'Supplier · glm-5.3', sourceLabel: 'Supplier', unlisted: true, removed: false });
  });

  it('keeps the unknown-model name only for a ledger key that no longer spells a model ID', () => {
    const foldedKey = `${'m'.repeat(200)}~${'0'.repeat(64)}`;
    // The ledger counts code points: 200 astral characters are still verbatim.
    const astralId = '🧠'.repeat(200);
    const value = reportWith([bucket('00', [row({ model_id: foldedKey }), row({ model_id: astralId })])]);
    expect(seriesFor(value, NO_FILTER, 'model', 'tokens', TEXT_ZH).map((item) => item.label))
      .toEqual(['Supplier · 未知模型', `Supplier · ${astralId}`]);
  });

  it('keeps a configured model name over an unlisted one that reads the same', () => {
    const value = reportWith([bucket('00', [row({ model_id: 'Model' }), row()])]);
    const labels = seriesFor(value, NO_FILTER, 'model', 'tokens', TEXT);
    expect(labels.map((item) => [item.key, item.label])).toEqual([
      [pairKey('source-a', 'Model'), 'Supplier · Model (2)'],
      [pairKey('source-a', 'model-a'), 'Supplier · Model'],
    ]);
  });

  // The report leaves a Source's label null exactly when config let it go.
  describe('Sources config let go', () => {
    const withRemoved = () => {
      const value = reportWith([
        bucket('00', [
          row({ source_id: 'src_d1f4adc47c3f', model_id: 'glm-5.3', requests: 3 }),
          row(),
        ]),
        bucket('01', [
          row({ source_id: 'src_d1f4adc47c3f', model_id: 'kimi-k2' }),
          row({ source_id: 'src_b52ba34d0659', model_id: 'deepseek-v3' }),
        ]),
      ]);
      value.sources.push(
        { source_id: 'src_d1f4adc47c3f', label: null, last_metered_at: null, ...counters(), models: [] },
        { source_id: 'src_b52ba34d0659', label: null, last_metered_at: null, ...counters(), models: [] },
      );
      return value;
    };

    it('MH-USAGE-034: fold into one last, muted series in either grouping, keeping every count', () => {
      const value = withRemoved();
      const total = seriesFor(value, NO_FILTER, 'total', 'requests', TEXT)[0]!.values;
      for (const group of ['model', 'source'] as const) {
        const series = seriesFor(value, NO_FILTER, group, 'requests', TEXT);
        expect(series.map((item) => item.label)).toEqual([group === 'model' ? 'Supplier · Model' : 'Supplier', 'Removed providers']);
        expect(series.at(-1)).toMatchObject({ key: REMOVED_SOURCES_KEY, removed: true, values: [3, 4] });
        expect(series.flatMap((item) => item.label)).not.toContainEqual(expect.stringContaining('src_'));
        expect(series.reduce((sum, item) => sum + (item.values[0] ?? 0) + (item.values[1] ?? 0), 0))
          .toBe((total[0] ?? 0) + (total[1] ?? 0));
      }
    });

    it('select every folded row when the aggregate is picked in either filter', () => {
      const value = withRemoved();
      for (const selection of [
        { sourceIds: [REMOVED_SOURCES_KEY], modelKeys: [] },
        { sourceIds: [], modelKeys: [REMOVED_SOURCES_KEY] },
      ]) {
        const rows = filteredRows(value, resolveUsageFilter(value, selection));
        expect(rows.map((item) => (isModelRow(item) ? item.model_id : item.service_id)).sort()).toEqual(['deepseek-v3', 'glm-5.3', 'kimi-k2']);
      }
      const live = filteredRows(value, resolveUsageFilter(value, { sourceIds: ['source-a'], modelKeys: [REMOVED_SOURCES_KEY] }));
      expect(live).toEqual([]);
    });

    // A selection outlives the report it was made in; the next one may no
    // longer offer what it names.
    it('MH-USAGE-035: reconcile a kept selection with what this report offers', () => {
      const live = reportWith([bucket('00', [row()])]);
      const removed = withRemoved();
      const livePair = pairKey('source-a', 'model-a');
      const removedPair = pairKey('src_d1f4adc47c3f', 'glm-5.3');
      // Config let the picked Source go: it, and any pair of it, reads as the aggregate.
      expect(foldUsageSelection(removed, { sourceIds: ['src_d1f4adc47c3f', 'src_b52ba34d0659'], modelKeys: [removedPair] }))
        .toEqual({ sourceIds: [REMOVED_SOURCES_KEY], modelKeys: [REMOVED_SOURCES_KEY] });
      // The aggregate was picked, and nothing folds into it any more.
      expect(foldUsageSelection(live, { sourceIds: [REMOVED_SOURCES_KEY], modelKeys: [REMOVED_SOURCES_KEY] }))
        .toEqual(NO_FILTER);
      // A removed Source with no metered row still offers the aggregate as a Source, never as a model.
      const unmetered = reportWith([bucket('00', [row()])]);
      unmetered.sources.push({ source_id: 'src_d1f4adc47c3f', label: null, last_metered_at: null, ...counters(), models: [] });
      expect(foldUsageSelection(unmetered, { sourceIds: [REMOVED_SOURCES_KEY], modelKeys: [REMOVED_SOURCES_KEY] }))
        .toEqual({ sourceIds: [REMOVED_SOURCES_KEY], modelKeys: [] });
      // A picked model, or Source, this report never metered is dropped; what it still offers stays.
      const later = reportWith([bucket('00', [row({ model_id: 'model-b' })])]);
      expect(foldUsageSelection(later, { sourceIds: ['source-a', 'source-z'], modelKeys: [livePair, pairKey('source-a', 'model-b')] }))
        .toEqual({ sourceIds: ['source-a'], modelKeys: [pairKey('source-a', 'model-b')] });
      // Nothing left picked means no filter, not a filter that matches nothing.
      expect(filteredRows(later, resolveUsageFilter(later, { sourceIds: [], modelKeys: [livePair] })))
        .toEqual(filteredRows(later, NO_FILTER));
    });

    it('never fold a live Source, even one that metered nothing in the window', () => {
      const value = reportWith([bucket('00', [row({ source_id: 'src_d1f4adc47c3f', model_id: 'glm-5.3' })])]);
      value.sources = [
        { source_id: 'source-a', label: 'Supplier', last_metered_at: null, ...counters(), models: [] },
        { source_id: 'src_d1f4adc47c3f', label: null, last_metered_at: null, ...counters(), models: [] },
      ];
      const identities = usageIdentities(value, 'source', TEXT);
      expect(identities.of([row({ source_id: 'source-a' }), row({ source_id: 'src_d1f4adc47c3f' })]).map((item) => item.label))
        .toEqual(['Supplier', 'Removed providers']);
    });
  });

  it('renders incomplete empty buckets as unavailable gaps, not zero', () => {
    const value = reportWith([bucket('00', [], false), bucket('01', [], false)]);
    const series = seriesFor(value, NO_FILTER, 'total', 'tokens', TEXT);
    expect(series[0]?.values).toEqual([null, null]);
    expect(usageIsEmpty(value)).toBe(false);
    expect(reportHasPartialHistory(value, { sourceIds: [], modelKeys: [] })).toBe(true);
  });

  it('includes both explicit offsets when formatting a bucket range', () => {
    const base = bucket('00', []);
    expect(formatBucketRange({
      ...base,
      start_at: '2026-11-01T01:00:00-04:00',
      end_at: '2026-11-01T01:00:00-05:00',
    }, 'en-US', true)).toContain('UTC-04:00');
    expect(formatBucketRange({
      ...base,
      start_at: '2026-11-01T01:00:00-04:00',
      end_at: '2026-11-01T01:00:00-05:00',
    }, 'en-US', true)).toContain('UTC-05:00');
  });

  // The tooltip heading is compact; the full, zone-qualified range stays in
  // the accessible label (formatBucketRange above).
  it('shortens a bucket heading to one date and names the zone only where it matters', () => {
    const at = (start_at: string, end_at: string, key = start_at): UsageBucket => ({ ...bucket('00', []), key, start_at, end_at });
    const hostOffset = (value: string) => -new Date(value).getTimezoneOffset();
    const hour = at('2026-09-23T22:00:00+08:00', '2026-09-23T23:00:00+08:00');
    const expectedZone = hostOffset(hour.start_at) === 8 * 60 ? null : 'UTC+08:00';
    expect(formatBucketHeading(hour, 'zh')).toEqual({ range: '9月23日 22:00–23:00', zone: expectedZone });
    expect(formatBucketHeading(hour, 'en-US').range).toBe('Sep 23, 22:00–23:00');
    // The last hour of a day ends at the next midnight, still one date.
    expect(formatBucketHeading(at('2026-09-23T23:00:00+08:00', '2026-09-24T00:00:00+08:00'), 'zh').range).toBe('9月23日 23:00–24:00');
    // A whole day reads as its date.
    expect(formatBucketHeading(at('2026-09-23T00:00:00+08:00', '2026-09-24T00:00:00+08:00', '2026-09-23'), 'zh').range).toBe('9月23日');
    // Two ends in different offsets always name both.
    expect(formatBucketHeading(at('2026-11-01T01:00:00-04:00', '2026-11-01T01:00:00-05:00'), 'en-US').zone).toBe('UTC-04:00 – UTC-05:00');
  });

  it('keeps axis labels compact while detail labels remain full', () => {
    const hourly = bucket('00', []);
    expect(formatBucketAxisLabel(hourly, 'en-US')).toBe('00:00');
    expect(formatBucketAxisLabel({
      ...hourly,
      key: '2026-09-24',
      start_at: '2026-09-24T00:00:00+08:00',
      end_at: '2026-09-25T00:00:00+08:00',
    }, 'en-US')).toBe('Sep 24');
    expect(formatBucketRange(hourly, 'en-US')).toContain('Sep 24, 2026');
  });

  // A total at API price is the sum of its rows, and it is a price only when
  // every row was priced: a server that predates valuation must not read as $0.
  it('MH-USAGE-028: sums API price only across rows that all carry one', () => {
    const priced = aggregateCounters([
      counters({ api_cost_usd: 1.25, excluded_tokens: 0, api_cost_lower_bound: false }),
      counters({ api_cost_usd: 0.5, excluded_tokens: 30, api_cost_lower_bound: true }),
    ]);
    expect(priced.api_cost_usd).toBeCloseTo(1.75);
    expect(priced.excluded_tokens).toBe(30);
    expect(priced.api_cost_lower_bound).toBe(true);
    expect(usageMetricValue(priced, 'cost')).toBeCloseTo(1.75);

    const mixed = aggregateCounters([counters({ api_cost_usd: 1 }), counters()]);
    expect(usageIsPriced(mixed)).toBe(false);
    expect(usageMetricValue(mixed, 'cost')).toBeNull();
    expect(usageMetricValue(counters({ requests: 0, token_reports: 0 }), 'cost')).toBe(0);
    expect(reportIsPriced(reportWith([]))).toBe(false);
    expect(reportIsPriced({ ...reportWith([]), pricing: { currency: 'USD', price_table_date: null } })).toBe(true);
  });
});

// A search request (`web-search-services.md` §4.3) shares a bucket with model
// calls but reaches no model: it counts toward cost alone.
describe('search-service usage', () => {
  afterEach(() => { vi.useRealTimers(); });

  const search = (over: Partial<UsageServiceBucketRow> = {}): UsageServiceBucketRow => ({
    kind: 'service',
    source_id: 'src_exa',
    service_id: 'exa/search',
    requests: 3,
    billable_requests: 2,
    uncertain_requests: 1,
    reported_requests: 2,
    reported_usd: 0.0135,
    api_cost_usd: 0.014,
    api_cost_lower_bound: true,
    ...over,
  });
  const listing = (entry: UsageServiceBucketRow, over: Partial<ServiceUsage>): ServiceUsage => ({
    source_id: entry.source_id,
    service_id: entry.service_id,
    label: null,
    service_label: null,
    last_metered_at: null,
    requests: entry.requests,
    billable_requests: entry.billable_requests,
    uncertain_requests: entry.uncertain_requests,
    reported_requests: entry.reported_requests,
    priced: true,
    ...over,
  });
  const exa = search();
  const tavily = search({
    source_id: 'src_tavily', service_id: 'tavily/search', requests: 1, billable_requests: 1, uncertain_requests: 0,
    reported_requests: 0, reported_usd: 0, api_cost_usd: 0.008, api_cost_lower_bound: false,
  });
  // A Source config let go, whose service a later catalog no longer prices.
  const retired = search({
    source_id: 'src_gone', service_id: 'retired/search', requests: 1, billable_requests: 1, uncertain_requests: 0,
    reported_requests: 0, reported_usd: 0, api_cost_usd: 0, api_cost_lower_bound: true,
  });
  const withServices = (): UsageReport => ({
    ...reportWith([
      bucket('00', [row({ api_cost_usd: 0.5, excluded_tokens: 0, api_cost_lower_bound: false }), exa, tavily]),
      bucket('01', [retired]),
    ]),
    services: [
      listing(exa, { label: 'Exa key' }),
      listing(tavily, { label: 'Tavily', service_label: 'Tavily' }),
      listing(retired, { priced: false }),
    ],
    pricing: { currency: 'USD', price_table_date: '2026-09-23', service_price_date: '2026-09-20' },
  });
  const values = (value: UsageReport, filter: UsageFilter, metric: 'cost' | 'requests') =>
    seriesFor(value, filter, 'total', metric, TEXT)[0]!.values.map((item) => (item === null ? null : Number(item.toFixed(6))));

  it('MH-USAGE-036: adds search cost under the Source filter, never under a model filter, and no call or token', () => {
    const value = withServices();
    const cost = seriesFor(value, NO_FILTER, 'total', 'cost', TEXT)[0]!;
    expect(values(value, NO_FILTER, 'cost')).toEqual([0.522, 0]);
    // A retired service's request is unpriced, never $0.
    expect([cost.floors, cost.unpriced]).toEqual([[true, true], [false, true]]);
    expect(values(value, NO_FILTER, 'requests')).toEqual([2, 0]);
    expect(values(value, { sourceIds: ['src_exa'], modelKeys: [] }, 'cost')).toEqual([0.014, 0]);
    expect(values(value, { sourceIds: ['src_exa'], modelKeys: [] }, 'requests')).toEqual([0, 0]);
    const model = { sourceIds: [], modelKeys: [pairKey('source-a', 'model-a')] };
    expect(filteredRows(value, model).every(isModelRow)).toBe(true);
    expect(values(value, model, 'cost')).toEqual([0.5, 0]);
    // Grouped, the services keep the stack equal to the total; only cost shows them.
    const labels = (group: 'model' | 'source', metric: 'cost' | 'tokens') => seriesFor(value, NO_FILTER, group, metric, TEXT).map((item) => item.label);
    expect(labels('model', 'cost')).toEqual(['Supplier · Model', 'Search services', 'Removed providers']);
    expect(labels('source', 'cost')).toEqual(['Supplier', 'Exa key', 'Tavily', 'Removed providers']);
    expect(labels('source', 'tokens')).toEqual(['Supplier']);
    // A tool-only Source is a Source to filter by, never a model.
    expect(foldUsageSelection(value, { sourceIds: ['src_exa'], modelKeys: [pairKey('src_exa', 'exa/search')] }))
      .toEqual({ sourceIds: ['src_exa'], modelKeys: [] });
  });

  it('MH-USAGE-036: reads service-only usage as usage, with no call or token in it', () => {
    const value = { ...reportWith([bucket('00', [exa])]), services: [listing(exa, { label: 'Exa key' })] };
    expect(usageIsEmpty(value)).toBe(false);
    expect(values(value, NO_FILTER, 'requests')).toEqual([0]);
    expect(seriesFor(value, NO_FILTER, 'total', 'tokens', TEXT)[0]!.values).toEqual([0]);
  });

  it('MH-USAGE-036: tables each live service by its catalog name and folds removed Sources, keeping their counts', () => {
    const value = withServices();
    const table = (filter: UsageFilter, pinned: string | null = null) => serviceUsageRows(value, filter, pinned, TEXT)
      .map(({ label, service, removed, totals }) => [label, service, removed, totals.requests, totals.billable_requests,
        totals.uncertain_requests, totals.api_cost_usd, totals.reported_usd, usageHasNoPrice(totals)]);
    expect(table(NO_FILTER)).toEqual([
      // No catalog name left for the service: its ID.
      ['Exa key', 'exa/search', false, 3, 2, 1, 0.014, 0.0135, false],
      ['Tavily', 'Tavily', false, 1, 1, 0, 0.008, 0, false],
      ['Removed providers', '', true, 1, 1, 0, 0, 0, true],
    ]);
    expect(table(NO_FILTER, '01').map(([label]) => label)).toEqual(['Removed providers']);
    expect(table({ sourceIds: [], modelKeys: [pairKey('source-a', 'model-a')] })).toEqual([]);
    expect(aggregateCounters(filteredRows(value, NO_FILTER).filter((entry) => !isModelRow(entry))).requests).toBe(0);
  });

  it('WF-1: separates fetch/search on the same service and never inflates search details', () => {
    const fetched = search({ service_id: 'exa/mcp', tool: 'web_fetch', requests: 7 });
    const searched = search({ service_id: 'exa/mcp', requests: 3 });
    const value = { ...reportWith([bucket('00', [searched, fetched])]), services: [
      listing(searched, { label: 'Exa (free)', service_label: 'Exa' }),
      listing(fetched, { label: 'Exa (free)', service_label: 'Exa', tool: 'web_fetch' }),
    ] };
    expect(serviceUsageRows(value, NO_FILTER, null, TEXT).map((item) => [item.tool ?? 'web_search', item.totals.requests]))
      .toEqual([['web_fetch', 7], ['web_search', 3]]);
    value.window_key = '60d'; value.granularity = 'day'; value.to_day = '2026-09-24';
    value.buckets![0]!.key = '2026-09-24';
    expect(serviceMonthUsage(value, 'src_exa').totals.requests).toBe(3);
    expect(serviceMonthUsage(value, 'src_exa', 'web_fetch').totals.requests).toBe(7);
    value.services = value.services.map((item) => ({ ...item, label: null }));
    const removed = serviceUsageRows(value, NO_FILTER, null, TEXT);
    expect(removed.map((item) => [item.removed, item.tool ?? 'web_search', item.totals.requests]))
      .toEqual([[true, 'web_fetch', 7], [true, 'web_search', 3]]);
  });

  describe('this month on a Source detail', () => {
    const DAY = 24 * 60 * 60 * 1000;
    // A 60-day daily report ending on the host's local today, `to_day`.
    const daily = (toDay: string, rows: Partial<Record<string, UsageBucketEntry[]>>, incomplete: readonly string[] = []): UsageReport => {
      const end = Date.parse(`${toDay}T00:00:00Z`);
      const keys = Array.from({ length: 60 }, (_, index) => new Date(end - (59 - index) * DAY).toISOString().slice(0, 10));
      return {
        ...reportWith(keys.map((key) => ({
          key, start_at: `${key}T00:00:00-07:00`, end_at: `${key}T23:59:59-07:00`, history_complete: !incomplete.includes(key), rows: rows[key] ?? [],
        }))),
        window_key: '60d', granularity: 'day', window_days: 60, from_day: keys[0]!, to_day: toDay,
        pricing: { currency: 'USD', price_table_date: '2026-09-23' },
      };
    };
    const one = (over: Partial<UsageServiceBucketRow> = {}) => search({ requests: 1, billable_requests: 1, uncertain_requests: 0, reported_requests: 0, reported_usd: 0, api_cost_usd: 0.007, api_cost_lower_bound: false, ...over });

    it.each([
      {
        name: 'a 31-day month, from its first day to the host\'s today',
        toDay: '2026-10-31',
        rows: { '2026-09-30': [one()], '2026-10-01': [one({ requests: 2, billable_requests: 2, api_cost_usd: 0.014 })], '2026-10-15': [one({ source_id: 'src_other' })], '2026-10-31': [one()] },
        incomplete: ['2026-09-30'],
        expected: { month: '2026-10', requests: 3, cost: 0.021, partial: false },
      },
      {
        // The browser's clock is still on October; the host's day is what counts.
        name: 'the host\'s new month, ahead of the browser\'s',
        toDay: '2026-11-01',
        rows: { '2026-10-31': [one({ requests: 5, billable_requests: 5 })], '2026-11-01': [one()] },
        incomplete: [],
        expected: { month: '2026-11', requests: 1, cost: 0.007, partial: false },
      },
      {
        name: 'a month with a day missing its history',
        toDay: '2026-10-20',
        rows: { '2026-10-02': [one()] },
        incomplete: ['2026-10-03'],
        expected: { month: '2026-10', requests: 1, cost: 0.007, partial: true },
      },
    ])('MH-USAGE-038: counts $name', ({ toDay, rows, incomplete, expected }) => {
      vi.useFakeTimers({ toFake: ['Date'] });
      vi.setSystemTime(new Date('2026-10-31T12:00:00-07:00'));
      const month = serviceMonthUsage(daily(toDay, rows, incomplete), 'src_exa');
      expect({ month: month.month, requests: month.totals.requests, cost: Number(month.totals.api_cost_usd?.toFixed(6)), partial: month.partial })
        .toEqual(expected);
      expect(month.priced).toBe(true);
    });
  });
});
