import { describe, expect, it } from 'vitest';

import type { UsageBucket, UsageBucketRow, UsageCounters, UsageReport } from './types';
import {
  aggregateCounters,
  filteredRows,
  formatBucketAxisLabel,
  formatBucketHeading,
  formatBucketRange,
  pairKey,
  REMOVED_SOURCES_KEY,
  reportHasPartialHistory,
  reportIsPriced,
  resolveUsageFilter,
  seriesFor,
  usageIdentities,
  usageIsEmpty,
  usageIsPriced,
  usageMetricValue,
  usageNonCachedInput,
  usageTotalTokens,
} from './usageProjection';

const TEXT = { unknownModel: 'Unknown model', removedSources: 'Removed providers' };
const TEXT_ZH = { unknownModel: '未知模型', removedSources: '已移除的供应商' };
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

const bucket = (key: string, rows: UsageBucketRow[], history_complete = true): UsageBucket => ({
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
    const value = reportWith([bucket('00', [row({ model_id: foldedKey })])]);
    expect(seriesFor(value, NO_FILTER, 'model', 'tokens', TEXT_ZH).map((item) => item.label)).toEqual(['Supplier · 未知模型']);
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
  describe('MH-USAGE-033: Sources config let go', () => {
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

    it.each(['model', 'source'] as const)('fold into one last, muted series when grouped by %s, keeping every count', (group) => {
      const value = withRemoved();
      const series = seriesFor(value, NO_FILTER, group, 'requests', TEXT);
      expect(series.map((item) => item.label)).toEqual([group === 'model' ? 'Supplier · Model' : 'Supplier', 'Removed providers']);
      expect(series.at(-1)).toMatchObject({ key: REMOVED_SOURCES_KEY, removed: true, values: [3, 4] });
      expect(series.flatMap((item) => item.label)).not.toContainEqual(expect.stringContaining('src_'));
      const total = seriesFor(value, NO_FILTER, 'total', 'requests', TEXT)[0]!.values;
      expect(series.reduce((sum, item) => sum + (item.values[0] ?? 0) + (item.values[1] ?? 0), 0))
        .toBe((total[0] ?? 0) + (total[1] ?? 0));
    });

    it('select every folded row when the aggregate is picked in either filter', () => {
      const value = withRemoved();
      for (const selection of [
        { sourceIds: [REMOVED_SOURCES_KEY], modelKeys: [] },
        { sourceIds: [], modelKeys: [REMOVED_SOURCES_KEY] },
      ]) {
        const rows = filteredRows(value, resolveUsageFilter(value, selection));
        expect(rows.map((item) => item.model_id).sort()).toEqual(['deepseek-v3', 'glm-5.3', 'kimi-k2']);
      }
      const live = filteredRows(value, resolveUsageFilter(value, { sourceIds: ['source-a'], modelKeys: [REMOVED_SOURCES_KEY] }));
      expect(live).toEqual([]);
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
