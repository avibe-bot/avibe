// 最近切换 as a feed rather than a snapshot: it grows at the HEAD while 加载更早
// pages from the TAIL, so both directions are merges into one accumulated list
// and neither may throw the other's rows away.
import type { ResolutionEvent } from './types';

/**
 * 最近切换 as the page knows it: the rows, and whether its far end has been seen.
 *
 * One value rather than two pieces of state because every transition moves both,
 * and the one that moved only the rows is the bug this type exists to make
 * unwriteable: a head read that REPLACES a gapped feed throws away the tail the
 * exhaustion flag was a claim about, and 加载更早 then offers no way back to rows
 * that certainly still exist.
 */
export type EventFeed = {
  events: ResolutionEvent[];
  /** Whether the TAIL has been reached, i.e. 加载更早 has nothing left to fetch. */
  exhausted: boolean;
};

/** Before anything has been read: no rows, and nothing to page back to yet. */
export const emptyFeed: EventFeed = { events: [], exhausted: true };

/**
 * One list from two pages of the same feed, in the order given, without repeats.
 *
 * ORDER IS POSITIONAL and has to be: `id` is `evt_<uuid4hex>`, so sorting by it
 * would scramble the feed into a random permutation that still looked plausible.
 * The endpoint returns newest-first (`BoundedEventLog.list` reverses the append
 * log), so the caller states the relationship by argument order — a head page
 * goes first, a tail page goes second — and this only removes the overlap.
 *
 * The overlap is normal, not a bug: a head re-read after a write returns rows we
 * already hold, and a tail page can be crossed by an event landing mid-request.
 * The FIRST argument wins a duplicate, which is what makes the head page the
 * fresher copy of a row it shares with what is on screen.
 */
export const mergeEventFeed = (
  first: ResolutionEvent[],
  second: ResolutionEvent[],
): ResolutionEvent[] => {
  const seen = new Set(first.map((event) => event.id));
  return [...first, ...second.filter((event) => !seen.has(event.id))];
};

/**
 * Whether a re-read head page has left a HOLE between itself and the rows already
 * on screen — the one case where merging the two would be a lie.
 *
 * The test is disjointness, not page fullness. A full head page proves nothing:
 * `listEvents(EVENT_PAGE)` returns the newest page whether or not anything is
 * new, so 「full」 is true of every feed with at least a page of history in it.
 * What actually distinguishes the bad case is that the newest page and the rows
 * we hold no longer touch: more than a page of events landed between the two
 * reads, so everything in between is missing, and splicing them together yields a
 * list where every row is well-formed and the sequence is fiction.
 *
 * An empty screen cannot have a hole (there is nothing to be apart from), and
 * neither can an empty head page — a feed that just answered with nothing has no
 * newer rows to be separated by.
 */
export const headReadGapped = (
  headPage: ResolutionEvent[],
  onScreen: ResolutionEvent[],
): boolean => {
  if (headPage.length === 0 || onScreen.length === 0) return false;
  const held = new Set(onScreen.map((event) => event.id));
  return !headPage.some((event) => held.has(event.id));
};

/**
 * The feed after a head re-read: merged onto what is on screen, or — when the two
 * no longer touch — replaced by the contiguous newest page.
 *
 * Replacing costs the user their 加载更早 rows, which is the honest price: those
 * rows are still reachable by paging, while a spliced feed is unreachable by any
 * action because nothing about it looks wrong.
 *
 * Which is only true if paging can still reach them, and that is why `exhausted`
 * is part of the answer. Merging leaves it alone — the tail is still on screen, so
 * a claim about the tail still holds, and this read was of the head. Replacing
 * clears it: the rows it discarded are older than everything it kept and they
 * exist by construction (disjoint pages mean a page-plus landed in between), so
 * 「there is nothing older」 is exactly what stopped being true. If the log has
 * since evicted them, the next 加载更早 comes back short and sets it again.
 */
export const feedAfterHeadRead = (onScreen: EventFeed, headPage: ResolutionEvent[]): EventFeed =>
  headReadGapped(headPage, onScreen.events)
    ? { events: headPage, exhausted: false }
    : {
        events: mergeEventFeed(headPage, onScreen.events),
        exhausted: onScreen.exhausted,
      };

/**
 * The feed after a read from the TAIL end — 加载更早, and the first page at mount,
 * which is a tail read too: it reaches the end exactly when it comes back short.
 *
 * `pageSize` is passed rather than baked in because the page owns that constant;
 * a short page is the only evidence either read has that there is nothing older.
 */
export const feedAfterTailRead = (
  onScreen: EventFeed,
  tailPage: ResolutionEvent[],
  pageSize: number,
): EventFeed => ({
  events: mergeEventFeed(onScreen.events, tailPage),
  exhausted: tailPage.length < pageSize,
});
