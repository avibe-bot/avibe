import { describe, expect, it } from 'vitest';

import type { UsageBucket, UsageBucketRow, UsageCounters, UsageReport } from './types';
import {
  formatBucketAxisLabel,
  formatBucketRange,
  identityLabel,
  pairKey,
  reportHasPartialHistory,
  seriesFor,
  usageIsEmpty,
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
    const series = seriesFor(value, { sourceIds: [], modelKeys: [] }, 'model', 'tokens');
    expect(series.map((item) => item.key)).toEqual([pairKey('source-a', 'model-a'), pairKey('source-b', 'model-a')]);
    expect(series.map((item) => item.label)).toEqual([
      'Supplier · Model · source-a',
      'Supplier · Model · source-b',
    ]);
  });

  it('localizes an unknown model in model-grouped series', () => {
    const value = reportWith([bucket('00', [row({ model_id: 'removed-model' })])]);
    expect(seriesFor(value, { sourceIds: [], modelKeys: [] }, 'model', 'tokens', 'zh-CN')[0]?.label).toBe('Supplier · 未知模型');
    expect(identityLabel({
      key: pairKey('source-a', 'removed-model'),
      sourceId: 'source-a',
      modelId: 'removed-model',
      sourceLabel: 'Supplier',
      modelLabel: null,
    }, 'zh-CN')).toBe('Supplier · 未知模型');
  });

  it('disambiguates localized unknown-model collisions', () => {
    const value = reportWith([bucket('00', [
      row({ model_id: 'removed-model' }),
      row({ model_id: 'model-a' }),
    ])]);
    value.sources[0]!.models = [
      { model_id: 'model-a', label: '未知模型', ...counters() },
    ];

    expect(seriesFor(value, { sourceIds: [], modelKeys: [] }, 'model', 'tokens', 'zh-CN').map((item) => item.label)).toEqual([
      'Supplier · 未知模型 · source-a · removed-model',
      'Supplier · 未知模型 · source-a · model-a',
    ]);
  });

  it('renders incomplete empty buckets as unavailable gaps, not zero', () => {
    const value = reportWith([bucket('00', [], false), bucket('01', [], false)]);
    const series = seriesFor(value, { sourceIds: [], modelKeys: [] }, 'total', 'tokens');
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
});
