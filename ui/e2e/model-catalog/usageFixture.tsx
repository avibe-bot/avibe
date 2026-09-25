import { useEffect, useMemo, useState } from 'react';

import { UsageTab } from '../../src/components/settings/models/UsageTab';
import { readyRegion } from '../../src/components/settings/models/regionRead';
import type { UsageBucketRow, UsageCounters, UsageReport, UsageWindowKey } from '../../src/components/settings/models/types';

const params = new URLSearchParams(location.search);
const CURRENT_AT = '2026-09-24T20:37:00+08:00';
const CURRENT_MS = Date.UTC(2026, 8, 24, 12, 37);
const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

const counters = (over: Partial<UsageCounters> = {}): UsageCounters => ({
  requests: 3,
  token_reports: 3,
  input_tokens: 120,
  cached_input_tokens: 40,
  output_tokens: 60,
  ...over,
});

const row = (over: Partial<UsageBucketRow> = {}): UsageBucketRow => ({
  ...counters(),
  source_id: 'source-alpha',
  model_id: 'model-shared',
  ...over,
});

const localAt = (ms: number): string => {
  const shifted = new Date(ms + 8 * HOUR_MS);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:00+08:00`;
};

const dayKey = (ms: number): string => localAt(ms).slice(0, 10);

const rowsFor = (index: number, count: number): UsageBucketRow[] => {
  if (index === 1) return [row({ source_id: 'source-alpha' })];
  if (index === Math.floor(count / 2)) return [row({ source_id: 'source-beta' })];
  if (index === count - 2) return [row({ source_id: 'source-alpha', model_id: 'model-second' })];
  return [];
};

const makeReport = (windowKey: UsageWindowKey): UsageReport => {
  const count = windowKey === '24h' ? 24 : Number(windowKey.slice(0, -1));
  const granularity = windowKey === '24h' ? 'hour' : 'day';
  const firstStart = windowKey === '24h'
    ? Date.UTC(2026, 8, 24, 12) - 23 * HOUR_MS
    : Date.UTC(2026, 8, 24) - 8 * HOUR_MS - (count - 1) * DAY_MS;
  const buckets = Array.from({ length: count }, (_, index) => {
    const startMs = firstStart + index * (granularity === 'hour' ? HOUR_MS : DAY_MS);
    const endMs = index === count - 1 ? CURRENT_MS : startMs + (granularity === 'hour' ? HOUR_MS : DAY_MS);
    return {
      key: granularity === 'day' ? dayKey(startMs) : localAt(startMs),
      start_at: localAt(startMs),
      end_at: localAt(endMs),
      history_complete: index !== count - 1,
      rows: rowsFor(index, count),
    };
  });
  return {
    window_days: windowKey === '24h' ? 1 : count,
    from_day: dayKey(firstStart),
    to_day: '2026-09-24',
    totals: counters({
      requests: 9,
      token_reports: 9,
      input_tokens: 360,
      cached_input_tokens: 120,
      output_tokens: 180,
    }),
    sources: [
      {
        source_id: 'source-alpha',
        label: 'Same supplier',
        last_metered_at: '2026-09-24T18:00:00+08:00',
        ...counters({ requests: 6, token_reports: 6, input_tokens: 240, cached_input_tokens: 80, output_tokens: 120 }),
        models: [
          { model_id: 'model-shared', label: 'Same model', ...counters() },
          { model_id: 'model-second', label: 'Second model', ...counters() },
        ],
      },
      {
        source_id: 'source-beta',
        label: 'Same supplier',
        last_metered_at: '2026-09-24T10:00:00+08:00',
        ...counters(),
        models: [{ model_id: 'model-shared', label: 'Same model', ...counters() }],
      },
    ],
    days: [],
    window_key: windowKey,
    granularity,
    from_at: localAt(firstStart),
    to_at: CURRENT_AT,
    buckets,
  };
};

export function UsageFixture() {
  const [windowKey, setWindowKey] = useState<UsageWindowKey>('24h');
  const usage = useMemo(() => readyRegion(makeReport(windowKey)), [windowKey]);

  useEffect(() => {
    const theme = params.get('theme');
    if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
    return () => {
      delete document.documentElement.dataset.theme;
    };
  }, []);

  return (
    <main
      style={{
        boxSizing: 'border-box',
        width: '100%',
        height: '100dvh',
        overflowY: 'auto',
        padding: '24px 0 48px',
      }}
    >
      <div style={{ width: 'min(1100px, calc(100vw - 32px))', margin: '0 auto' }}>
        <UsageTab usage={usage} windowKey={windowKey} onWindowChange={setWindowKey} />
      </div>
    </main>
  );
}
