import { describe, expect, it } from 'vitest';

import { cooldownEtaMinutes, currencySymbol, formatNameList, formatSpend } from './format';

describe('currencySymbol', () => {
  it('falls back to USD when the backend reports no currency', () => {
    expect(currencySymbol(null)).toBe('$');
    expect(currencySymbol(undefined)).toBe('$');
    expect(currencySymbol()).toBe('$');
  });

  it('honors an explicitly reported currency', () => {
    expect(currencySymbol('USD')).toBe('$');
    expect(currencySymbol('CNY')).toBe('¥');
    expect(currencySymbol('EUR')).toBe('€');
  });

  it('returns no symbol for a code it cannot map', () => {
    expect(currencySymbol('JPY')).toBe('');
  });

  it('agrees with the symbol formatSpend uses, for every currency', () => {
    for (const currency of [null, undefined, 'USD', 'CNY', 'EUR', 'JPY'] as const) {
      expect(formatSpend(1240, currency).startsWith(currencySymbol(currency))).toBe(true);
      const stripped = formatSpend(1240, currency).slice(currencySymbol(currency).length);
      expect(stripped).toBe('12.4');
    }
  });
});

describe('formatSpend', () => {
  it('falls back to USD when the backend reports no currency', () => {
    expect(formatSpend(1240, null)).toBe('$12.4');
    expect(formatSpend(1240, undefined)).toBe('$12.4');
    expect(formatSpend(1240)).toBe('$12.4');
  });

  it('honors an explicitly reported currency', () => {
    expect(formatSpend(1240, 'USD')).toBe('$12.4');
    expect(formatSpend(1240, 'CNY')).toBe('¥12.4');
    expect(formatSpend(1240, 'EUR')).toBe('€12.4');
  });

  it('omits the symbol for a currency it cannot map', () => {
    expect(formatSpend(1240, 'JPY')).toBe('12.4');
  });

  it('converts cents to one decimal without applying any FX rate', () => {
    expect(formatSpend(0, null)).toBe('$0.0');
    expect(formatSpend(5, null)).toBe('$0.1');
    expect(formatSpend(1234567, null)).toBe('$12345.7');
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
