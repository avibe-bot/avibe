import { describe, expect, it } from 'vitest';

import { cooldownEtaMinutes, currencySymbol, formatSpend } from './format';

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
    // Callers use '' to pick a currency-free label instead of printing a wrong
    // symbol — the amount cell carries the authoritative value.
    expect(currencySymbol('JPY')).toBe('');
  });

  // The billing chip renders currencySymbol(...) while the usage cell renders
  // formatSpend(...). They must never disagree: a static '$' on the chip read
  // "按量 $" beside "¥12.4" for a CNY source (Codex P2, 2026-07-25).
  it('agrees with the symbol formatSpend uses, for every currency', () => {
    for (const c of [null, undefined, 'USD', 'CNY', 'EUR', 'JPY'] as const) {
      expect(formatSpend(1240, c).startsWith(currencySymbol(c))).toBe(true);
      const stripped = formatSpend(1240, c).slice(currencySymbol(c).length);
      expect(stripped).toBe('12.4');
    }
  });
});

describe('formatSpend', () => {
  // Owner ruling 2026-07-25: upstream vendors bill in USD, so a missing currency
  // must render as USD and the UI must never fall back to a local currency.
  // This is the tripwire for that decision — if someone restores a CNY (or any
  // other) fallback, this fails instead of shipping silently.
  it('falls back to USD when the backend reports no currency', () => {
    expect(formatSpend(1240, null)).toBe('$12.4');
    expect(formatSpend(1240, undefined)).toBe('$12.4');
    expect(formatSpend(1240)).toBe('$12.4');
  });

  it('honors an explicitly reported currency', () => {
    // The ISO 4217 map is deliberately retained: a source that genuinely bills in
    // CNY/EUR still renders in its own currency.
    expect(formatSpend(1240, 'USD')).toBe('$12.4');
    expect(formatSpend(1240, 'CNY')).toBe('¥12.4');
    expect(formatSpend(1240, 'EUR')).toBe('€12.4');
  });

  it('omits the symbol for a currency it cannot map, keeping the amount readable', () => {
    expect(formatSpend(1240, 'JPY')).toBe('12.4');
  });

  it('converts cents to one decimal without applying any FX rate', () => {
    // 1240 cents stays 12.40 whatever the symbol — amounts are never converted.
    expect(formatSpend(0, null)).toBe('$0.0');
    expect(formatSpend(5, null)).toBe('$0.1');
    expect(formatSpend(1234567, null)).toBe('$12345.7');
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
