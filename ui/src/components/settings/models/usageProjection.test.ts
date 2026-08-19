import { describe, expect, it } from 'vitest';

import { USAGE_WINDOW_MAX_DAYS, USAGE_WINDOW_MIN_DAYS, type UsageByModel, type UsageBySource, type UsageCounters, type UsageSummary } from './types';
import {
  USAGE_WINDOW_OPTIONS,
  formatLocalDay,
  modelIdentity,
  parseLocalDay,
  sourceIdentity,
  usageCachedInputShare,
  usageDayColumns,
  usageIsEmpty,
  usageReportShortfall,
  usageTotalTokens,
} from './usageProjection';

const counters = (over: Partial<UsageCounters> = {}): UsageCounters => ({
  requests: 0,
  token_reports: 0,
  input_tokens: 0,
  cached_input_tokens: 0,
  output_tokens: 0,
  ...over,
});

const model = (over: Partial<UsageByModel> = {}): UsageByModel => ({
  ...counters(),
  model_id: 'claude-opus-4-6',
  label: 'claude-opus-4-6',
  ...over,
});

const source = (over: Partial<UsageBySource> = {}): UsageBySource => ({
  ...counters(),
  source_id: 'src_conform001',
  label: 'Contract source',
  last_metered_at: null,
  models: [model()],
  ...over,
});

const summary = (over: Partial<UsageSummary> = {}): UsageSummary => ({
  window_days: 30,
  from_day: '2026-07-20',
  to_day: '2026-08-18',
  totals: counters(),
  sources: [],
  days: [],
  ...over,
});

describe('USAGE_WINDOW_OPTIONS', () => {
  // The reason this file exists: an option outside the contract bounds is a
  // number on screen the report never covered.
  it('offers only windows the contract can serve', () => {
    expect(USAGE_WINDOW_OPTIONS.length).toBeGreaterThan(0);
    for (const option of USAGE_WINDOW_OPTIONS) {
      expect(Number.isInteger(option)).toBe(true);
      expect(option).toBeGreaterThanOrEqual(USAGE_WINDOW_MIN_DAYS);
      expect(option).toBeLessThanOrEqual(USAGE_WINDOW_MAX_DAYS);
    }
  });

  it('offers each window once, shortest first', () => {
    const options = [...USAGE_WINDOW_OPTIONS];
    expect(new Set(options).size).toBe(options.length);
    expect(options).toEqual([...options].sort((left, right) => left - right));
  });
});

describe('usageTotalTokens', () => {
  it('adds input and output without subtracting the cached subset', () => {
    expect(usageTotalTokens(counters({ input_tokens: 148_230, cached_input_tokens: 96_010, output_tokens: 4_120 }))).toBe(152_350);
  });
});

describe('usageReportShortfall', () => {
  it('counts the requests that carried no token report', () => {
    expect(usageReportShortfall(counters({ requests: 12, token_reports: 9 }))).toBe(3);
    expect(usageReportShortfall(counters({ requests: 12, token_reports: 12 }))).toBe(0);
  });

  it('never reports a negative shortfall, whatever the payload claims', () => {
    expect(usageReportShortfall(counters({ requests: 4, token_reports: 9 }))).toBe(0);
  });
});

describe('usageCachedInputShare', () => {
  it('is the cached share of reported input', () => {
    expect(usageCachedInputShare(counters({ input_tokens: 200, cached_input_tokens: 50 }))).toBe(0.25);
  });

  it('has no share to report when nothing was input', () => {
    expect(usageCachedInputShare(counters({ input_tokens: 0, cached_input_tokens: 0 }))).toBeNull();
    expect(usageCachedInputShare(counters({ input_tokens: 0, cached_input_tokens: 90 }))).toBeNull();
  });

  it('stays a share even when the payload breaks the subset promise', () => {
    expect(usageCachedInputShare(counters({ input_tokens: 100, cached_input_tokens: 400 }))).toBe(1);
    expect(usageCachedInputShare(counters({ input_tokens: 100, cached_input_tokens: -40 }))).toBe(0);
  });
});

describe('usageIsEmpty', () => {
  it('is empty exactly when no Source was metered', () => {
    expect(usageIsEmpty(summary())).toBe(true);
    expect(usageIsEmpty(summary({ sources: [source()] }))).toBe(false);
  });
});

describe('parseLocalDay', () => {
  it('accepts a contract LocalDay and rejects anything else', () => {
    expect(parseLocalDay('2026-08-18')).toBe(Date.UTC(2026, 7, 18));
    for (const rejected of ['', '2026-8-18', '2026-08-18T00:00:00Z', 'yesterday', '20260818']) {
      expect(parseLocalDay(rejected)).toBeNull();
    }
  });

  // `Date.parse` rolls these forward instead of refusing them, so a day that
  // survives must read back identical rather than merely parse.
  it('rejects a day the calendar does not have', () => {
    for (const rolled of ['2026-02-30', '2026-13-01', '2026-04-31', '2026-00-10', '2026-01-32']) {
      expect(parseLocalDay(rolled)).toBeNull();
    }
  });

  it('round-trips every day of the widest window it will plot', () => {
    const first = parseLocalDay('2026-01-01');
    expect(first).not.toBeNull();
    for (let index = 0; index < USAGE_WINDOW_MAX_DAYS; index += 1) {
      const day = new Date((first as number) + index * 86_400_000).toISOString().slice(0, 10);
      expect(parseLocalDay(day)).not.toBeNull();
    }
  });
});

describe('formatLocalDay', () => {
  it('keeps the labelled day when read back, in either locale', () => {
    expect(formatLocalDay('2026-08-18', 'en-US')).toContain('18');
    expect(formatLocalDay('2026-08-18', 'en-US')).toContain('2026');
    expect(formatLocalDay('2026-08-18', 'zh-CN')).toContain('18');
    expect(formatLocalDay('2026-08-18', 'zh-CN')).toContain('2026');
  });

  it('renders an unparseable day verbatim rather than inventing one', () => {
    expect(formatLocalDay('not-a-day', 'en-US')).toBe('not-a-day');
  });
});

describe('usageDayColumns', () => {
  const day = (value: string, tokens: number) => ({ ...counters({ input_tokens: tokens }), day: value });

  it('spans the whole window and zero-fills the days that carried no turn', () => {
    const columns = usageDayColumns(summary({ from_day: '2026-08-14', to_day: '2026-08-18', days: [day('2026-08-16', 40)] }));
    expect(columns.map((column) => column.day)).toEqual(['2026-08-14', '2026-08-15', '2026-08-16', '2026-08-17', '2026-08-18']);
    expect(columns.map((column) => column.tokens)).toEqual([0, 0, 40, 0, 0]);
  });

  // The property, not a case list: whatever the reported set is, every reported
  // day keeps its own tokens and nothing else gains any.
  it('carries the tokens of every reported day through unchanged', () => {
    const reported = [day('2026-08-14', 10), day('2026-08-16', 40), day('2026-08-18', 25)];
    const columns = usageDayColumns(summary({ from_day: '2026-08-14', to_day: '2026-08-18', days: reported }));
    const byDay = new Map(columns.map((column) => [column.day, column.tokens]));
    for (const entry of reported) expect(byDay.get(entry.day)).toBe(usageTotalTokens(entry));
    const total = columns.reduce((sum, column) => sum + column.tokens, 0);
    expect(total).toBe(reported.reduce((sum, entry) => sum + usageTotalTokens(entry), 0));
  });

  it('scales every bar against the busiest day on screen', () => {
    const columns = usageDayColumns(summary({ from_day: '2026-08-17', to_day: '2026-08-18', days: [day('2026-08-17', 25), day('2026-08-18', 100)] }));
    expect(columns.map((column) => column.ratio)).toEqual([0.25, 1]);
  });

  it('gives a silent window flat bars rather than a division by zero', () => {
    const columns = usageDayColumns(summary({ from_day: '2026-08-17', to_day: '2026-08-18' }));
    expect(columns.map((column) => column.ratio)).toEqual([0, 0]);
  });

  it('falls back to the reported days when the window bounds are unusable', () => {
    // Reversed, malformed, and wider than retention all mean the same thing: the
    // span is not ours to trust, so plot what was actually reported.
    for (const window of [
      { from_day: '2026-08-18', to_day: '2026-08-14' },
      { from_day: 'nope', to_day: '2026-08-18' },
      { from_day: '2020-01-01', to_day: '2026-08-18' },
    ]) {
      const columns = usageDayColumns(summary({ ...window, days: [day('2026-08-16', 40)] }));
      expect(columns.map((column) => column.day)).toEqual(['2026-08-16']);
      expect(columns[0].tokens).toBe(40);
    }
  });

  it('never plots more columns than the maximum window the contract allows', () => {
    const columns = usageDayColumns(summary({ from_day: '2026-06-18', to_day: '2026-08-18' }));
    expect(columns.length).toBeLessThanOrEqual(USAGE_WINDOW_MAX_DAYS);
  });
});

describe('usage row identity', () => {
  it('shows the joined label while there is one', () => {
    expect(sourceIdentity(source({ label: 'Contract source' }))).toEqual({ kind: 'label', text: 'Contract source' });
    expect(modelIdentity(model({ label: 'claude-opus-4-6' }))).toEqual({ kind: 'label', text: 'claude-opus-4-6' });
  });

  it('treats a blank label as no label at all', () => {
    expect(sourceIdentity(source({ label: '   ' })).kind).toBe('gone');
    expect(modelIdentity(model({ label: '' })).kind).toBe('gone');
  });

  it('keeps a gone Source addressable by its canonical id', () => {
    expect(sourceIdentity(source({ label: null, source_id: 'src_gone' }))).toEqual({ kind: 'gone', id: 'src_gone' });
  });

  // The contract's rule, as a property: `model_id` is a head plus a digest for
  // any long identifier, so it is never a string this UI may display.
  it('never offers the ledger key of a gone model as something to display', () => {
    for (const key of ['claude-opus-4-6', 'anthropic/claude-opus-4-6-2026#9f2a1c', 'x'.repeat(200)]) {
      const identity = modelIdentity(model({ label: null, model_id: key }));
      expect(identity).toEqual({ kind: 'gone', id: null });
      expect(JSON.stringify(identity)).not.toContain(key);
    }
  });
});
