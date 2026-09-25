// 订阅额度 — pure derivations over `quota-summary.schema.json`.
//
// Everything here is a function of the payload and one `now`, so the tab can be
// drawn and tested against a fixed instant. Copy lives in the tab; this module
// only decides which sentence applies and with which numbers.
import type { QuotaWindow, SourceQuota } from './types';

export const MINUTE_MS = 60_000;
export const HOUR_MS = 60 * MINUTE_MS;
export const DAY_MS = 24 * HOUR_MS;

export type QuotaTone = 'ok' | 'warn' | 'danger' | 'stale';

/** A duration in whole units, largest two first — the shape the copy states. */
export type QuotaDuration =
  | { unit: 'days'; days: number; hours: number }
  | { unit: 'hours'; hours: number; minutes: number }
  | { unit: 'minutes'; minutes: number };

export function quotaDuration(ms: number): QuotaDuration {
  // Whole minutes, rounded up: a countdown never claims less time than remains.
  const total = Math.max(1, Math.ceil(ms / MINUTE_MS));
  const days = Math.floor(total / (24 * 60));
  const hours = Math.floor((total % (24 * 60)) / 60);
  const minutes = total % 60;
  if (days) return { unit: 'days', days, hours };
  if (hours) return { unit: 'hours', hours, minutes };
  return { unit: 'minutes', minutes };
}

const instant = (value: string | null): number | null => {
  if (value === null) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
};

export const windowResetAt = (window: QuotaWindow): number | null => instant(window.resets_at);

/**
 * A Source with a current reading, not a retained one. A report with no windows
 * is not a reading: every summary figure and the card share this predicate.
 */
export const quotaIsLive = (source: SourceQuota): boolean => source.state === 'ok' && source.windows.length > 0;

/** A Source whose windows are the last good reading, shown under a banner. */
export const quotaIsRetained = (source: SourceQuota): boolean =>
  (source.state === 'stale' || source.state === 'auth_expired') && source.fetched_at !== null && source.windows.length > 0;

export const windowIsScoped = (window: QuotaWindow): boolean => window.kind === 'model_weekly';

/** `true` once the window's reset instant has come; the reading predates it. */
export function windowHasReset(window: QuotaWindow, now: number): boolean {
  const resetAt = windowResetAt(window);
  return resetAt !== null && resetAt <= now;
}

/**
 * Share used as of `now`. A window past its reset has given its quota back, so
 * it reads as unused until the next read: every figure, bar, and status derives
 * from this one value so none can claim a reset beside a spent number.
 */
export const windowUsedNow = (window: QuotaWindow, now: number): number =>
  windowHasReset(window, now) ? 0 : window.used_pct;

/** `true` while the window is spent and has not yet reached its reset. */
export const windowIsExhausted = (window: QuotaWindow, now: number): boolean => windowUsedNow(window, now) >= 100;

export type QuotaPace =
  | { kind: 'paused' }
  | { kind: 'reset' }
  | { kind: 'exhausted'; remainingMs: number | null }
  | { kind: 'fast'; runsOutAt: number }
  | { kind: 'slightly_fast' }
  | { kind: 'ok' };

export type QuotaPaceReading = { pace: QuotaPace; tone: QuotaTone; elapsedPct: number | null };

/**
 * How the window is being spent against its own clock.
 *
 * The thin mark on the bar is the share of the window that has elapsed; a bar
 * past the mark is spending faster than time. "Fast" is reserved for a pace that
 * would actually run the window out before it resets, with an 8-point margin so
 * the first minutes of a fresh window do not flag every request.
 */
export function windowPace(window: QuotaWindow, now: number, retained: boolean): QuotaPaceReading {
  const resetAt = windowResetAt(window);
  const length = window.window_seconds !== null ? window.window_seconds * 1000 : null;
  const remaining = resetAt !== null ? resetAt - now : null;
  const elapsed = remaining !== null && length !== null ? Math.min(1, Math.max(0, 1 - remaining / length)) : null;
  const elapsedPct = elapsed !== null ? Math.round(elapsed * 100) : null;
  const used = windowUsedNow(window, now);
  if (retained) return { pace: { kind: 'paused' }, tone: 'stale', elapsedPct };
  if (windowHasReset(window, now)) return { pace: { kind: 'reset' }, tone: 'ok', elapsedPct };
  if (used >= 100) return { pace: { kind: 'exhausted', remainingMs: remaining }, tone: 'danger', elapsedPct };
  if (elapsed === null || elapsedPct === null || remaining === null || length === null) {
    return { pace: { kind: 'ok' }, tone: 'ok', elapsedPct };
  }
  const rate = used / Math.max(elapsed, 0.02);
  const toFull = rate > 0 ? ((100 - used) / rate) * length : Infinity;
  if (used > elapsedPct + 8 && toFull < remaining) {
    return { pace: { kind: 'fast', runsOutAt: now + toFull }, tone: 'warn', elapsedPct };
  }
  return { pace: { kind: used > elapsedPct ? 'slightly_fast' : 'ok' }, tone: 'ok', elapsedPct };
}

export type QuotaStatus =
  | { kind: 'ok' }
  | { kind: 'exhausted'; window: QuotaWindow }
  | { kind: 'reauth' }
  | { kind: 'paused' };

export function sourceStatus(source: SourceQuota, now: number): QuotaStatus {
  if (source.state === 'auth_expired') return { kind: 'reauth' };
  if (!quotaIsLive(source)) return { kind: 'paused' };
  const spent = source.windows.find((window) => windowIsExhausted(window, now));
  return spent ? { kind: 'exhausted', window: spent } : { kind: 'ok' };
}

export type QuotaPick = { source: SourceQuota; window: QuotaWindow };

const livePicks = (sources: SourceQuota[]): QuotaPick[] =>
  sources.filter(quotaIsLive).flatMap((source) => source.windows.map((window) => ({ source, window })));

/** The live window with the least left that is still usable. */
export function tightestWindow(sources: SourceQuota[], now: number): QuotaPick | null {
  return livePicks(sources)
    .filter(({ window }) => windowUsedNow(window, now) < 100)
    .sort((a, b) => windowUsedNow(b.window, now) - windowUsedNow(a.window, now))[0] ?? null;
}

/** Live windows that are spent right now, soonest recovery first. */
export function exhaustedWindows(sources: SourceQuota[], now: number): QuotaPick[] {
  return livePicks(sources)
    .filter(({ window }) => windowIsExhausted(window, now))
    .sort((a, b) => (windowResetAt(a.window) ?? Infinity) - (windowResetAt(b.window) ?? Infinity));
}

/** Live windows resetting within a week that have something to give back. */
export function upcomingResets(sources: SourceQuota[], now: number, limit = 5): QuotaPick[] {
  return livePicks(sources)
    .filter(({ window }) => {
      const resetAt = windowResetAt(window);
      return windowUsedNow(window, now) > 0 && resetAt !== null && resetAt - now < 7 * DAY_MS;
    })
    .sort((a, b) => (windowResetAt(a.window) ?? 0) - (windowResetAt(b.window) ?? 0))
    .slice(0, limit);
}

/** Whole percent left as of `now`, never negative; any headroom rounds up so it never reads as 0% left. */
export const windowLeftPct = (window: QuotaWindow, now: number): number =>
  Math.max(0, Math.ceil(100 - windowUsedNow(window, now)));

/** Whole percent used, the complement of `windowLeftPct`: a limit with headroom never reads as 100% used. */
export const windowUsedPct = (window: QuotaWindow, now: number): number => 100 - windowLeftPct(window, now);

/**
 * How an API-price valuation compares with the plan's monthly fee.
 *
 * `multiple` is value ÷ fee. From 1.1× the plan has clearly paid for itself; from
 * 1× to just under 1.1× it has only just done so, and a multiple that rounds to
 * 「1.0 倍」 is not a figure worth boasting; below 1× the gap is the dollars
 * still to go.
 */
export type QuotaPayback =
  | { kind: 'paid'; multiple: number; surplusUsd: number }
  | { kind: 'even'; multiple: number; surplusUsd: number }
  | { kind: 'short'; multiple: number; shortfallUsd: number };

export const PAYBACK_CLEAR_MULTIPLE = 1.1;

export function quotaPayback(costUsd: number, feeUsd: number): QuotaPayback | null {
  if (!(feeUsd > 0) || !Number.isFinite(costUsd)) return null;
  const multiple = costUsd / feeUsd;
  if (multiple >= PAYBACK_CLEAR_MULTIPLE) return { kind: 'paid', multiple, surplusUsd: costUsd - feeUsd };
  if (multiple >= 1) return { kind: 'even', multiple, surplusUsd: costUsd - feeUsd };
  return { kind: 'short', multiple, shortfallUsd: feeUsd - costUsd };
}
