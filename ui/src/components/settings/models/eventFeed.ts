// 最近切换 as a feed rather than a snapshot: it grows at the HEAD while 加载更早
// pages from the TAIL, so both directions are merges into one accumulated list
// and neither may throw the other's rows away.
import type { ResolutionEvent } from './types';

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
 */
export const feedAfterHeadRead = (
  onScreen: ResolutionEvent[],
  headPage: ResolutionEvent[],
): ResolutionEvent[] =>
  headReadGapped(headPage, onScreen) ? headPage : mergeEventFeed(headPage, onScreen);
