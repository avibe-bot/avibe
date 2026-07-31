// Pure formatting helpers for the Model Hub UI (no i18n — callers wrap the
// returned values in translated templates).
const CURRENCY_SYMBOL: Record<string, string> = { USD: '$', CNY: '¥', EUR: '€' };

/**
 * Symbol for an ISO 4217 code — the ONLY place a currency symbol is resolved.
 *
 * USD is the fallback because every upstream vendor bills in USD; a CNY default
 * was our own invention. The mapping is kept, so a source that really does report
 * CNY (or EUR) still renders in its own currency. Returns '' for a code we cannot
 * map, so callers can fall back to a currency-free label rather than print a
 * misleading symbol.
 */
export function currencySymbol(currency?: string | null): string {
  return CURRENCY_SYMBOL[currency ?? 'USD'] ?? '';
}

/** Monthly spend as symbol + amount (1 decimal), e.g. "$12.4". */
export function formatSpend(cents: number, currency?: string | null): string {
  return `${currencySymbol(currency)}${(cents / 100).toFixed(1)}`;
}

/**
 * A list of names in the reader's own punctuation — 「A、B、C」 for a Chinese
 * reader, "A, B, C" for an English one.
 *
 * Joining on a literal `、` looked locale-neutral and is not: it is Chinese
 * punctuation, and it shipped into the English UI. `narrow` + `conjunction` is the
 * pair that separates in BOTH locales and adds an "and" to neither — `unit` joins
 * Chinese with nothing at all, and the wider styles grow a 和 / "and" that an
 * 11px attribution line has no room for.
 */
export function formatNameList(names: readonly string[], locale: string): string {
  return new Intl.ListFormat(locale, { style: 'narrow', type: 'conjunction' }).format(names);
}

/** Whole minutes until a cooldown retry_at (never negative). */
export function cooldownEtaMinutes(retryAt?: string | null): number {
  if (!retryAt) return 0;
  const ms = new Date(retryAt).getTime() - Date.now();
  return Math.max(0, Math.round(ms / 60_000));
}
