// Pure projections over `usage-summary.schema.json` for the 用量 tab.
//
// Everything here is a derivation the tab could get WRONG, kept out of JSX so it
// can be asserted as a property: what the window spans, what a row is allowed to
// display, and how a sparse trend becomes a dense one. Layout and copy stay in
// the component; this module never translates.
//
// It owns the trailing local-day window — `YYYY-MM-DD` labels in and out.
// `localCalendar.ts` answers a different question (is this INSTANT today?) over a
// different input type, so the two are deliberately not merged: an instant needs
// a timezone to become a day, and these strings already are days.
import { USAGE_WINDOW_MAX_DAYS, USAGE_WINDOW_MIN_DAYS, type UsageByDay, type UsageByModel, type UsageBySource, type UsageCounters, type UsageSummary } from './types';

/**
 * The windows the tab offers.
 *
 * Bounded by the schema rather than by the design, which offers 7/30/90: the
 * server clamps `days` to retention and answers with what it served, so a 「90
 * 天」 option would put a number on screen the report never covered. The gate is
 * mechanical — `usageProjection.test.ts` fails if an option leaves the contract
 * bounds — so the next person to add one cannot reintroduce the lie by hand.
 */
export const USAGE_WINDOW_OPTIONS = [7, 30, 60] as const;
export type UsageWindowOption = (typeof USAGE_WINDOW_OPTIONS)[number];

const DAY_MS = 86_400_000;
const LOCAL_DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

const toLocalDay = (ms: number): string => new Date(ms).toISOString().slice(0, 10);

/**
 * A `LocalDay` as the UTC instant of its midnight, or null when it is not one.
 *
 * UTC is the arithmetic frame on purpose. The string is already a local calendar
 * day, so re-deriving it through the host's zone would let a DST boundary or an
 * offset change move a labelled day by one; stepping in fixed 86 400 s units and
 * reading the label straight back cannot.
 *
 * The pattern alone does not decide it. `Date.parse` rolls an out-of-range day
 * FORWARD rather than refusing it — `2026-02-30` becomes March 2 — and a rolled
 * day silently stops matching the label it was keyed by. Reading the day back out
 * and requiring it unchanged is a check against our own output, so no calendar
 * quirk of the host parser can pass through it.
 */
export function parseLocalDay(day: string): number | null {
  if (!LOCAL_DAY_PATTERN.test(day)) return null;
  const ms = Date.parse(`${day}T00:00:00Z`);
  return Number.isNaN(ms) || toLocalDay(ms) !== day ? null : ms;
}

/** A `LocalDay` in the reader's own date order — 「2026年7月20日」/「Jul 20, 2026」.
 *  Formatted in UTC for the same reason the arithmetic is: the label must survive
 *  the round trip unshifted. Unparseable input renders verbatim rather than as an
 *  invented date. */
export function formatLocalDay(day: string, locale: string): string {
  const ms = parseLocalDay(day);
  if (ms === null) return day;
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeZone: 'UTC' }).format(ms);
}

/** Tokens the window actually moved. Cache is NOT subtracted: a cached input
 *  token was still composed into the request, and `cached_input_tokens` is a
 *  subset of `input_tokens` reported separately so a view can qualify the total
 *  without double-counting it. */
export function usageTotalTokens(counters: UsageCounters): number {
  return counters.input_tokens + counters.output_tokens;
}

/**
 * Requests whose upstream response carried no token report.
 *
 * The schema guarantees `token_reports <= requests`, and this floors at zero
 * anyway: both numbers arrive over the wire, and a view that renders 「-3 次未回
 * 报」 has turned a payload bug into a claim about the user's usage. The honest
 * reading of a shortfall is missing reports, never unused capacity — which is
 * why the caller's copy says so.
 */
export function usageReportShortfall(counters: UsageCounters): number {
  return Math.max(0, counters.requests - counters.token_reports);
}

/** Share of input tokens served from cache, or null when there is no input to
 *  take a share of. Clamped to [0, 1] because the subset relation is the
 *  server's promise, not something this view can verify. */
export function usageCachedInputShare(counters: UsageCounters): number | null {
  if (counters.input_tokens <= 0) return null;
  return Math.min(1, Math.max(0, counters.cached_input_tokens / counters.input_tokens));
}

/** Nothing was metered in the window. `sources` is the gate because it is what
 *  the table renders, and a Source enters it only once it has a metered turn. */
export function usageIsEmpty(summary: UsageSummary): boolean {
  return summary.sources.length === 0;
}

export type UsageDayColumn = {
  day: string;
  /** Calls metered on this day, whether or not their tokens came back. */
  requests: number;
  tokens: number;
  /** Height against the busiest rendered day, in [0, 1]. */
  ratio: number;
};

/**
 * Whether a day carried a metered call at all.
 *
 * A day's activity is its request counter, never its token total. An upstream
 * that answered without a token report still served a call the user made, so a
 * day of those is a day that ran — and drawing it at zero height, or folding it
 * into 「没有任何一天有计量数据」, reports our own missing evidence as the user's
 * idleness. That is the rule the requests card states as a shortfall, applied per
 * day, and it lives here so the series has one place to ask rather than one
 * `tokens > 0` per thing it draws.
 */
export function usageDayIsMetered(column: UsageDayColumn): boolean {
  return column.requests > 0;
}

/**
 * Every day of the window, oldest first, with the days that carried no turn
 * filled in at zero.
 *
 * `days[]` is sparse by contract — a quiet day is absent, not reported as zero —
 * and a chart drawn straight from it would silently close the gaps and read as
 * continuous traffic. Densifying from `from_day`/`to_day` is what makes an idle
 * stretch visible as an idle stretch.
 *
 * The span comes from the dates rather than from `window_days` so a payload
 * whose two disagree still plots its real dates, and it is refused outright past
 * the contract's own maximum: the bound is ours, measured against the schema, so
 * a `from_day` far in the past cannot ask this for a million columns. A refused
 * span falls back to exactly the days reported — fewer bars, no invention.
 */
export function usageDayColumns(summary: UsageSummary): UsageDayColumn[] {
  const reportedByDay = new Map(summary.days.map((day: UsageByDay) => [day.day, day]));
  const span = windowSpan(summary.from_day, summary.to_day);
  const days = span ?? summary.days.map((day) => day.day);
  // Both counters travel, because tokens alone cannot tell a day nobody used
  // from a day whose upstream never said what it cost.
  const counted = days.map((day) => {
    const reported = reportedByDay.get(day);
    return { day, requests: reported?.requests ?? 0, tokens: reported ? usageTotalTokens(reported) : 0 };
  });
  // Scaled against the days on screen, not every day reported: a stray row
  // outside the window would otherwise flatten every bar the user can see.
  const peak = counted.reduce((max, column) => Math.max(max, column.tokens), 0);
  return counted.map((column) => ({ ...column, ratio: peak > 0 ? column.tokens / peak : 0 }));
}

const windowSpan = (fromDay: string, toDay: string): string[] | null => {
  const from = parseLocalDay(fromDay);
  const to = parseLocalDay(toDay);
  if (from === null || to === null || to < from) return null;
  const length = (to - from) / DAY_MS + 1;
  if (!Number.isInteger(length) || length < USAGE_WINDOW_MIN_DAYS || length > USAGE_WINDOW_MAX_DAYS) return null;
  return Array.from({ length }, (_, index) => toLocalDay(from + index * DAY_MS));
};

/**
 * What a usage row may put on screen for its own identity.
 *
 * `gone` carries an id only where one is safe to show. A Source keeps its
 * canonical `src_*` id, which is a string the user could have seen elsewhere. A
 * model's ledger key is a head plus a digest for any long identifier — a string
 * nobody typed — so a vanished model has NO displayable identity and gets a bare
 * marker. That asymmetry is the reason both live behind one type instead of two
 * `label ?? id` expressions in JSX.
 */
export type UsageIdentity = { kind: 'label'; text: string } | { kind: 'gone'; id: string | null };

const identify = (label: string | null, fallbackId: string | null): UsageIdentity => {
  const text = label?.trim();
  // A blank label is as unrenderable as a missing one, and the schema permits it.
  return text ? { kind: 'label', text } : { kind: 'gone', id: fallbackId };
};

export function sourceIdentity(source: UsageBySource): UsageIdentity {
  return identify(source.label, source.source_id);
}

export function modelIdentity(model: UsageByModel): UsageIdentity {
  return identify(model.label, null);
}
