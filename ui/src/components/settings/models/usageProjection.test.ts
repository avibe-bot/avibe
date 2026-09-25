import { describe, expect, it } from 'vitest';

import type { UsageBucket, UsageBucketRow, UsageCounters, UsageReport } from './types';
import {
  aggregateCounters,
  formatBucketAxisLabel,
  formatBucketHeading,
  formatBucketRange,
  identityLabel,
  pairKey,
  reportHasPartialHistory,
  reportIsPriced,
  seriesFor,
  sourceIdentityLabel,
  usageLabelContext,
  usageIsEmpty,
  usageIsPriced,
  usageMetricValue,
  usageNonCachedInput,
  usageTotalTokens,
} from './usageProjection';

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

  it('keeps source/model identity separate even when labels match', () => {
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
    const series = seriesFor(value, { sourceIds: [], modelKeys: [] }, 'model', 'tokens', 'Unknown model');
    expect(series.map((item) => item.key)).toEqual([pairKey('source-a', 'model-a'), pairKey('source-b', 'model-a')]);
    expect(series.map((item) => item.label)).toEqual([
      'Supplier · Model · source-a',
      'Supplier · Model · source-b',
    ]);
  });

  it('localizes an unknown model in model-grouped series', () => {
    const value = reportWith([bucket('00', [row({ model_id: 'removed-model' })])]);
    expect(seriesFor(value, { sourceIds: [], modelKeys: [] }, 'model', 'tokens', '未知模型')[0]?.label).toBe('Supplier · 未知模型');
    expect(identityLabel({
      key: pairKey('source-a', 'removed-model'),
      sourceId: 'source-a',
      modelId: 'removed-model',
      sourceLabel: 'Supplier',
      modelLabel: null,
    }, '未知模型')).toBe('Supplier · 未知模型');
  });

  it('disambiguates localized unknown-model collisions', () => {
    const value = reportWith([bucket('00', [
      row({ model_id: 'removed-model' }),
      row({ model_id: 'model-a' }),
    ])]);
    value.sources[0]!.models = [
      { model_id: 'model-a', label: '未知模型', ...counters() },
    ];

    expect(seriesFor(value, { sourceIds: [], modelKeys: [] }, 'model', 'tokens', '未知模型').map((item) => item.label)).toEqual([
      'Supplier · 未知模型 · source-a · removed-model',
      'Supplier · 未知模型 · source-a · model-a',
    ]);
  });

  it('keeps final source labels unique when a suffix collides with a literal label', () => {
    const value = reportWith([
      bucket('00', [
        row({ source_id: 'source-a' }),
        row({ source_id: 'source-b' }),
        row({ source_id: 'source-c' }),
      ]),
    ]);
    value.sources = [
      { source_id: 'source-a', label: 'Provider', last_metered_at: null, ...counters(), models: [] },
      { source_id: 'source-b', label: 'Provider', last_metered_at: null, ...counters(), models: [] },
      { source_id: 'source-c', label: 'Provider · source-a', last_metered_at: null, ...counters(), models: [] },
    ];
    const context = usageLabelContext(value, value.buckets.flatMap((bucket) => bucket.rows), 'Unknown model');
    const labels = ['source-a', 'source-b', 'source-c'].map((sourceId) => (
      sourceIdentityLabel(value, sourceId, context)
    ));

    expect(new Set(labels).size).toBe(labels.length);
    expect(labels.every((label) => label.length > 0)).toBe(true);
  });

  it('keeps final identity labels unique after collision suffixes are added', () => {
    const value = reportWith([
      bucket('00', [
        row({ model_id: 'old-a' }),
        row({ model_id: 'old-b' }),
        row({ source_id: 'source-b', model_id: 'literal-model' }),
      ]),
    ]);
    value.sources = [
      {
        source_id: 'source-a',
        label: 'Supplier',
        last_metered_at: null,
        ...counters(),
        models: [],
      },
      {
        source_id: 'source-b',
        label: 'Supplier',
        last_metered_at: null,
        ...counters(),
        models: [{
          model_id: 'literal-model',
          label: 'Unknown model · source-a · old-a',
          ...counters(),
        }],
      },
    ];

    const labels = seriesFor(
      value,
      { sourceIds: [], modelKeys: [] },
      'model',
      'tokens',
      'Unknown model',
    ).map((series) => series.label);

    expect(new Set(labels).size).toBe(labels.length);
    expect(labels).toContain('Supplier · Unknown model · source-a · old-a');
  });

  it('renders incomplete empty buckets as unavailable gaps, not zero', () => {
    const value = reportWith([bucket('00', [], false), bucket('01', [], false)]);
    const series = seriesFor(value, { sourceIds: [], modelKeys: [] }, 'total', 'tokens', 'Unknown model');
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
