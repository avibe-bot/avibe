// Pure formatting helpers for the Model Hub UI (no i18n — callers wrap the
// returned values in translated templates).

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

/**
 * A counted quantity in the reader's own grouping — 「2,968,500」.
 *
 * Grouped rather than compacted (「3M」) because a usage report is read to be
 * reconciled against a vendor's own console, and a rounded figure cannot be.
 * `Intl` is what makes the separator the reader's rather than ours; the value is
 * floored because a token count is a count, and a fractional one would be a bug
 * upstream rendered as truth here.
 */
export function formatCount(value: number, locale: string): string {
  return new Intl.NumberFormat(locale).format(Math.max(0, Math.floor(value)));
}

/**
 * A context window as the figure a model is published under — 「128K」.
 *
 * Compact, and compacted the same way in every locale, because this is not a
 * quantity being reconciled: it is the number that appears on the vendor's own
 * model card, in its docs and in every comparison the reader has already seen,
 * and 「12.8万」 is that number in a unit no provider uses. The exact value is one
 * field below in the reader's own grouping, so nothing is hidden by rounding
 * here. `null` renders as nothing rather than as a zero the catalog never
 * claimed.
 */
export function formatTokensCompact(value: number | null): string {
  if (value === null || value < 1) return '';
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 0 })
    .format(Math.floor(value));
}

/** A ratio in [0, 1] as a whole-percent string — 「65%」. */
export function formatPercent(ratio: number, locale: string): string {
  return new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 0 }).format(ratio);
}

/**
 * An instant as a short day-and-time in the reader's own zone — 「8/18 03:14」.
 *
 * The host zone is the frame on purpose: what this answers is "when did this last
 * happen to me", and a UTC stamp is a time the reader cannot reconcile against
 * their own day. The year is dropped because the report this appears in spans at
 * most a couple of months, so the year is never the ambiguous part; the month is
 * kept because the day alone is. An unparseable stamp renders verbatim rather
 * than as an invented time.
 */
export function formatDayTime(instant: string, locale: string): string {
  const ms = Date.parse(instant);
  if (Number.isNaN(ms)) return instant;
  return new Intl.DateTimeFormat(locale, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(ms);
}

/** Whole minutes until a cooldown retry_at (never negative). */
export function cooldownEtaMinutes(retryAt?: string | null): number {
  if (!retryAt) return 0;
  const ms = new Date(retryAt).getTime() - Date.now();
  return Math.max(0, Math.round(ms / 60_000));
}
