// Which stretches of a conversation the local message store holds in full
// (docs/plans/local-sync-store.md section 4.2). Pure functions, so the rules
// that decide what the device may later show as complete can be tested without
// IndexedDB.
//
// A row's position is the server's transcript order: ``coalesce(delivered_at,
// created_at)`` compared as text, then the id (storage/messages_service.py
// ``transcript_order_value``). Comparing the same strings the same way is what
// makes a local interval mean the same rows the server's interval does.
//
// A range says "every transcript row of this session whose key lies between
// ``from`` and ``to`` is on this device". ``from: null`` means the start of
// history. ``to`` is always the key of a row a server page returned, or null
// for a session the server answered as empty; coverage never reaches past rows
// it has seen. ``atTail`` adds "and nothing newer existed", which only holds
// while the event stream that would have delivered anything newer stayed
// continuous: ``streamToken`` names that stream, and a range whose token is not
// the current one has no tail claim.

export type OrderKey = { at: string; id: string };

export type CoverageRange = {
  from: OrderKey | null;
  to: OrderKey | null;
  atTail: boolean;
  streamToken: string | null;
  verifiedAt: number;
};

/** What a server page proved: the rows it returned, from where to where. */
export type PageReach = {
  /** Lowest key the page speaks for; null when nothing is older (no ``next_before_id``). */
  low: OrderKey | null;
  /** Highest key the page speaks for; null when nothing is newer (a tail). */
  high: OrderKey | null;
  first: OrderKey | null;
  last: OrderKey | null;
};

export const orderKeyOf = (row: { id: string; delivered_at?: string | null; created_at?: string | null }): OrderKey => ({
  at: row.delivered_at || row.created_at || '',
  id: row.id,
});

export function compareKeys(a: OrderKey, b: OrderKey): number {
  if (a.at !== b.at) return a.at < b.at ? -1 : 1;
  if (a.id !== b.id) return a.id < b.id ? -1 : 1;
  return 0;
}

const minFrom = (a: OrderKey | null, b: OrderKey | null) => (a === null || b === null ? null : compareKeys(a, b) <= 0 ? a : b);

const maxTo = (a: OrderKey | null, b: OrderKey | null) => {
  if (a === null) return b;
  if (b === null) return a;
  return compareKeys(a, b) >= 0 ? a : b;
};

/** Whether ``key`` lies inside ``[low, high]``, a null bound being open. */
export function withinReach(key: OrderKey, low: OrderKey | null, high: OrderKey | null): boolean {
  return (low === null || compareKeys(key, low) >= 0) && (high === null || compareKeys(key, high) <= 0);
}

/** Whether a range's tail claim still holds under the current stream. */
export const tailHolds = (range: CoverageRange, streamToken: string): boolean =>
  range.atTail && range.streamToken === streamToken;

/** Whether ``range`` and ``[low, high]`` share any key. A range whose tail claim
 *  holds reaches to the open end; one whose claim lapsed ends at ``to``. Keys
 *  are row positions, so touching at one key means sharing that row. */
function overlaps(range: CoverageRange, low: OrderKey | null, high: OrderKey | null, streamToken: string): boolean {
  const rangeHigh = tailHolds(range, streamToken) ? null : range.to;
  if (range.to === null && !tailHolds(range, streamToken)) return false;
  const startsBeforeHigh = high === null || range.from === null || compareKeys(range.from, high) <= 0;
  const endsAfterLow = low === null || rangeHigh === null || compareKeys(rangeHigh, low) >= 0;
  return startsBeforeHigh && endsAfterLow;
}

/**
 * The ranges after a page that proved ``reach`` landed. The page replaced
 * everything inside its reach, so a range that overlaps the reach is contiguous
 * with it and joins it; a range that does not stays apart, and the keys between
 * them stay a gap. A page that reaches the tail takes the tail claim from every
 * other range, and keeps it only with ``tailClaim``: a read that started before
 * the current stream did cannot speak for what the stream between dropped.
 */
export function mergePageCoverage(
  ranges: readonly CoverageRange[],
  reach: PageReach,
  { streamToken, now, tailClaim = true }: { streamToken: string; now: number; tailClaim?: boolean },
): CoverageRange[] {
  const atTail = reach.high === null;
  let merged: CoverageRange = {
    from: reach.low === null ? null : reach.first,
    to: reach.last,
    atTail: atTail && tailClaim,
    streamToken: atTail && tailClaim ? streamToken : null,
    verifiedAt: now,
  };
  const kept: CoverageRange[] = [];
  for (const range of ranges) {
    if (!overlaps(range, reach.low, reach.high, streamToken)) {
      kept.push(atTail ? { ...range, atTail: false, streamToken: null } : range);
      continue;
    }
    const extendsAbove = reach.high !== null
      && (tailHolds(range, streamToken) || (range.to !== null && compareKeys(range.to, reach.high) > 0));
    merged = {
      from: minFrom(merged.from, range.from),
      to: extendsAbove ? maxTo(merged.to, range.to) : merged.to,
      atTail: extendsAbove ? tailHolds(range, streamToken) : merged.atTail,
      streamToken: extendsAbove ? (tailHolds(range, streamToken) ? streamToken : null) : merged.streamToken,
      verifiedAt: Math.min(merged.verifiedAt, range.verifiedAt),
    };
  }
  return [...kept, merged].sort(compareRanges);
}

/** The ranges after a streamed row arrived. It extends the range whose tail
 *  claim holds; anywhere else it is a row the store has seen, not coverage. */
export function extendTailCoverage(
  ranges: readonly CoverageRange[],
  key: OrderKey,
  streamToken: string,
): CoverageRange[] {
  return ranges.map((range) => {
    if (!tailHolds(range, streamToken)) return range;
    if (range.to !== null && compareKeys(key, range.to) <= 0) return range;
    return { ...range, to: key };
  });
}

/** Whether ``key`` lies inside a stretch the store holds in full. */
export function isCovered(ranges: readonly CoverageRange[], key: OrderKey, streamToken: string): boolean {
  return ranges.some((range) => {
    if (range.from !== null && compareKeys(key, range.from) < 0) return false;
    if (tailHolds(range, streamToken)) return true;
    return range.to !== null && compareKeys(key, range.to) <= 0;
  });
}

function compareRanges(a: CoverageRange, b: CoverageRange): number {
  if (a.from === null) return b.from === null ? 0 : -1;
  if (b.from === null) return 1;
  return compareKeys(a.from, b.from);
}
