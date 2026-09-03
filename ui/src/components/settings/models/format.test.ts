import { describe, expect, it } from 'vitest';

import { cooldownEtaMinutes, formatCount, formatDayTime, formatNameList, formatPercent, formatTokensCompact } from './format';

describe('formatCount', () => {
  it('groups so a figure can be read against a vendor console', () => {
    expect(formatCount(2_968_500, 'en-US')).toBe('2,968,500');
    expect(formatCount(2_968_500, 'zh-CN')).toBe('2,968,500');
  });

  it('leaves a small count ungrouped', () => {
    expect(formatCount(0, 'en-US')).toBe('0');
    expect(formatCount(12, 'en-US')).toBe('12');
  });

  // A token count is a count: rounding it up, or rendering a negative one, would
  // put a claim on screen the ledger never made.
  it('never renders a fractional or negative count', () => {
    expect(formatCount(12.9, 'en-US')).toBe('12');
    expect(formatCount(-5, 'en-US')).toBe('0');
  });
});

describe('formatTokensCompact', () => {
  // The property: every window renders as the K/M figure the model is published
  // under — never grouped, never fractional, and never in a unit (万) no
  // provider's model card uses.
  it('renders the published K/M figure', () => {
    for (const value of [128_000, 163_840, 200_000, 1_047_576]) {
      expect(formatTokensCompact(value)).toMatch(/^\d+[KM]$/);
    }
    expect(formatTokensCompact(128_000)).toBe('128K');
    expect(formatTokensCompact(1_000_000)).toBe('1M');
  });

  it('renders nothing for a window nobody stated', () => {
    expect(formatTokensCompact(null)).toBe('');
    expect(formatTokensCompact(0)).toBe('');
  });
});

describe('formatPercent', () => {
  it('renders a share as whole percent', () => {
    expect(formatPercent(0, 'en-US')).toBe('0%');
    expect(formatPercent(0.647, 'en-US')).toBe('65%');
    expect(formatPercent(1, 'en-US')).toBe('100%');
  });
});

describe('formatDayTime', () => {
  it('keeps the day and the minute of the instant it was given', () => {
    // Fixed as an offset-bearing stamp so the assertion is about the formatter
    // rather than about the machine the test happens to run on.
    const rendered = formatDayTime('2026-08-18T03:14:00+00:00', 'en-US');
    const local = new Date('2026-08-18T03:14:00+00:00');
    expect(rendered).toContain(String(local.getDate()));
    expect(rendered).toContain(String(local.getMinutes()).padStart(2, '0'));
  });

  it('renders an unparseable stamp verbatim rather than inventing a time', () => {
    expect(formatDayTime('never', 'en-US')).toBe('never');
    expect(formatDayTime('', 'en-US')).toBe('');
  });
});

describe('formatNameList', () => {
  it('separates with the punctuation of the reader', () => {
    expect(formatNameList(['agent-a', 'agent-b'], 'zh')).toBe('agent-a、agent-b');
    expect(formatNameList(['agent-a', 'agent-b'], 'en')).toBe('agent-a, agent-b');
  });

  it('works from regional locale tags', () => {
    expect(formatNameList(['a', 'b'], 'zh-CN')).toBe('a、b');
    expect(formatNameList(['a', 'b'], 'en-US')).toBe('a, b');
  });

  it('stays a plain separated list at three names', () => {
    expect(formatNameList(['a', 'b', 'c'], 'zh')).toBe('a、b、c');
    expect(formatNameList(['a', 'b', 'c'], 'en')).toBe('a, b, c');
  });

  it('adds nothing around a single name', () => {
    expect(formatNameList(['only'], 'zh')).toBe('only');
    expect(formatNameList(['only'], 'en')).toBe('only');
  });
});

describe('cooldownEtaMinutes', () => {
  it('is 0 for a missing or already-elapsed retry_at', () => {
    expect(cooldownEtaMinutes(null)).toBe(0);
    expect(cooldownEtaMinutes(undefined)).toBe(0);
    expect(cooldownEtaMinutes(new Date(Date.now() - 60_000).toISOString())).toBe(0);
  });

  it('rounds the remaining wait to whole minutes', () => {
    expect(cooldownEtaMinutes(new Date(Date.now() + 5 * 60_000 + 1_000).toISOString())).toBe(5);
  });
});
