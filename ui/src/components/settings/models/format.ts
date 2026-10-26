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
 * Sentences read one after another, joined as the locale joins them: a space
 * in English, nothing in Chinese. `Intl` owns the joiner, as it does above.
 */
export function formatSentences(sentences: readonly string[], locale: string): string {
  return new Intl.ListFormat(locale, { style: 'narrow', type: 'unit' }).format(sentences);
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

/** A ratio in [0, 1] in the reader's notation, with the caller's display precision. */
export function formatPercent(ratio: number, locale: string, fractionDigits = 0): string {
  return new Intl.NumberFormat(locale, {
    style: 'percent',
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(ratio);
}

/**
 * A US-dollar figure in the reader's own grouping — 「$1,234.56」.
 *
 * Two decimals because an API-price valuation is compared against a monthly fee
 * quoted in dollars and cents; a sub-cent figure shows as 「<$0.01」 rather than
 * as a zero that reads as "free".
 */
export function formatUsd(value: number, locale: string): string {
  const format = (amount: number) => new Intl.NumberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
  if (value > 0 && value < 0.005) return `<${format(0.01)}`;
  return format(Math.max(0, value));
}

/**
 * A priced figure that may be only a floor. Every $ or multiple the value
 * surfaces show goes through this with the server's `api_cost_lower_bound`, so a
 * floor never reads as exact, a floor of zero included (「≥ $0.00」).
 */
export function atLeast(text: string, lowerBound: boolean | undefined): string {
  return lowerBound === true ? `≥ ${text}` : text;
}

/** `formatUsd` for a figure that carries its lower-bound flag. */
export function formatCost(value: number, lowerBound: boolean | undefined, locale: string): string {
  return atLeast(formatUsd(value, locale), lowerBound);
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

/**
 * A small per-call dollar figure with its first significant digits: $0.005, not
 * $0.01, since a hosted tool's price is often under a cent. A figure of a dime or
 * more keeps two decimals; one below a hundredth of a cent keeps two significant
 * digits ($0.000012), so a nonzero price never reads as $0.
 */
export function formatSmallUsd(value: number): string {
  if (value >= 0.1) return value.toFixed(2);
  const fixed = value >= 0.001 ? value.toFixed(3)
    : value === 0 || value >= 0.0001 ? value.toFixed(4)
    // Below a hundredth of a cent, two significant digits, so no listed price reads as $0.
    : Number(value.toPrecision(2)).toFixed(20);
  return fixed.replace(/0+$/, '').replace(/\.$/, '');
}

/**
 * The local calendar day as `MM-DD`, the stamp the capability chips carry
 * ("verified 10-13"). Both locales use the same numeric form, so the chip reads
 * the same width in either. An unparseable stamp renders verbatim.
 */
export const cooldownTime = (until: string, language: string): string => {
  const date = new Date(until);
  const clock = new Intl.DateTimeFormat(language, { hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
  return date.toDateString() === new Date().toDateString() ? clock : `${formatMonthDay(until)} ${clock}`;
};

export function formatMonthDay(instant: string): string {
  const ms = Date.parse(instant);
  if (Number.isNaN(ms)) return instant;
  const date = new Date(ms);
  return `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

/** Whole minutes until a cooldown retry_at (never negative). */
export function cooldownEtaMinutes(retryAt?: string | null): number {
  if (!retryAt) return 0;
  const ms = new Date(retryAt).getTime() - Date.now();
  return Math.max(0, Math.round(ms / 60_000));
}
