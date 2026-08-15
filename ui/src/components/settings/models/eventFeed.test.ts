// The feed merge, in both directions.
//
// The regression these pin: 最近切换 was fetched once at mount, so a write that
// filed an event (a failing 试跑 cooling its own head down) changed the source row
// while the line explaining that change stayed invisible until a full reload. The
// post-write refresh now re-reads the head — which is only safe if it cannot throw
// away the rows 加载更早 已经 paged in, and cannot splice a hole into the sequence.
//
// And the follow-on: the rows and 「is there anything older」 are one fact. Moving
// only the rows is how the replace branch left 加载更早 claiming an end it had just
// discarded, over history that certainly still existed.
//
// And its own follow-on, one interleaving further out: that replacement can happen
// while a tail read is in flight, and a tail page is only about the feed it was
// asked of. Merged into the replacement it splices two ends together with the
// middle missing — and unlike a replacement, unrecoverably, because paging goes on
// from the old cursor.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  emptyFeed,
  feedAfterHeadRead,
  feedAfterTailRead,
  feedTailCursor,
  headReadGapped,
  mergeEventFeed,
  type EventFeed,
} from './eventFeed';
import type { ResolutionEvent } from './types';

/** A feed row: only `id` matters to the merge, and only identity to the rest. */
const ev = (id: string): ResolutionEvent =>
  ({ id, ts: '2026-07-30T10:00:00Z', agent: 'claude', kind: 'cooldown' }) as ResolutionEvent;

const ids = (events: ResolutionEvent[]) => events.map((e) => e.id);

/** On-screen feed with the tail already reached, unless said otherwise. */
const onScreen = (rows: ResolutionEvent[], exhausted = true): EventFeed => ({
  events: rows,
  exhausted,
});

describe('mergeEventFeed — one list from two pages of the same feed', () => {
  it('keeps the given order and drops the overlap', () => {
    expect(ids(mergeEventFeed([ev('c'), ev('b')], [ev('b'), ev('a')]))).toEqual(['c', 'b', 'a']);
  });

  it('lets the FIRST argument win a duplicated row', () => {
    // Which is what makes a head re-read the fresher copy of a row already held.
    const fresh = { ...ev('b'), kind: 'recover' } as ResolutionEvent;
    const merged = mergeEventFeed([fresh], [ev('b')]);
    expect(merged).toHaveLength(1);
    expect(merged[0].kind).toBe('recover');
  });

  it('is a no-op for an empty page in either position', () => {
    expect(ids(mergeEventFeed([ev('a')], []))).toEqual(['a']);
    expect(ids(mergeEventFeed([], [ev('a')]))).toEqual(['a']);
  });

  it('never sorts by id, because ids do not order', () => {
    // `evt_<uuid4hex>`: sorting would scramble the feed into a plausible-looking
    // random permutation. Given newest-first pages, output order is positional.
    const merged = mergeEventFeed([ev('evt_ff'), ev('evt_00')], [ev('evt_aa')]);
    expect(ids(merged)).toEqual(['evt_ff', 'evt_00', 'evt_aa']);
  });
});

describe('headReadGapped — whether the two pages still touch', () => {
  it('is false when the head page overlaps what is on screen', () => {
    expect(headReadGapped([ev('d'), ev('c')], [ev('c'), ev('b'), ev('a')])).toBe(false);
  });

  it('is TRUE when more than a page landed in between', () => {
    // Nothing shared: every row between the two is missing, and a merge would read
    // as one continuous history.
    expect(headReadGapped([ev('z'), ev('y')], [ev('c'), ev('b')])).toBe(true);
  });

  it('is not about page fullness', () => {
    // A full head page is true of every feed with a page of history in it, new or
    // not — the discriminator this deliberately is not.
    const held = [ev('c'), ev('b'), ev('a')];
    expect(headReadGapped([ev('d'), ev('c'), ev('b')], held)).toBe(false);
  });

  it('finds no hole against an empty screen or an empty page', () => {
    expect(headReadGapped([ev('a')], [])).toBe(false);
    expect(headReadGapped([], [ev('a')])).toBe(false);
  });
});

describe('feedAfterHeadRead — what the post-write refresh leaves on screen', () => {
  it('merges the new rows in front of the paged-in tail', () => {
    const held = onScreen([ev('c'), ev('b'), ev('a')]);
    expect(ids(feedAfterHeadRead(held, [ev('d'), ev('c')]).events)).toEqual(['d', 'c', 'b', 'a']);
  });

  it('replaces rather than splices when the pages no longer touch', () => {
    // The paged rows are still reachable by paging; a spliced feed is reachable by
    // nothing, because no row in it looks wrong.
    const held = onScreen([ev('c'), ev('b')]);
    expect(ids(feedAfterHeadRead(held, [ev('z'), ev('y')]).events)).toEqual(['z', 'y']);
  });

  it('holds the screen when the head read came back empty', () => {
    expect(ids(feedAfterHeadRead(onScreen([ev('b'), ev('a')]), []).events)).toEqual(['b', 'a']);
  });

  it('keeps a merge from touching exhaustion, in either state', () => {
    // A claim about the TAIL, and the tail is still on screen after a merge.
    expect(feedAfterHeadRead(onScreen([ev('b'), ev('a')], true), [ev('c'), ev('b')]).exhausted).toBe(
      true,
    );
    expect(
      feedAfterHeadRead(onScreen([ev('b'), ev('a')], false), [ev('c'), ev('b')]).exhausted,
    ).toBe(false);
  });

  it('CLEARS exhaustion when it replaces a gapped feed', () => {
    // The regression: 加载更早 disappeared over rows that certainly still exist,
    // because the flag outlived the list it was a claim about. Disjoint pages mean
    // a page-plus landed in between, so the discarded rows are older than
    // everything kept — 「nothing older」 is precisely what stopped being true.
    expect(feedAfterHeadRead(onScreen([ev('c'), ev('b')], true), [ev('z')]).exhausted).toBe(false);
  });
});

describe('feedTailCursor — the row a tail read continues from', () => {
  it('is the last row on screen', () => {
    expect(feedTailCursor(onScreen([ev('c'), ev('b'), ev('a')]))).toBe('a');
  });

  it('is null for a feed with no tail yet, which is what reading the top is', () => {
    expect(feedTailCursor(emptyFeed)).toBeNull();
  });
});

describe('feedAfterTailRead — 加载更早, and the first page at mount', () => {
  it('appends the older page and reads the end off a short one', () => {
    const held = onScreen([ev('c'), ev('b')], false);
    const next = feedAfterTailRead(held, [ev('b'), ev('a')], 20, 'b');
    expect(ids(next.events)).toEqual(['c', 'b', 'a']);
    expect(next.exhausted).toBe(true);
  });

  it('keeps paging open on a full page', () => {
    expect(feedAfterTailRead(onScreen([ev('b')], true), [ev('a')], 1, 'b').exhausted).toBe(false);
  });

  it('reads the mount page as the tail it also is', () => {
    // Same owner as 加载更早 deliberately: reaching the end is the same question,
    // and a hand-rolled second copy is how the two drifted apart. Its cursor is
    // null because it asked for the top, which is what an empty feed's tail is.
    const first = feedAfterTailRead(emptyFeed, [ev('b'), ev('a')], 20, null);
    expect(ids(first.events)).toEqual(['b', 'a']);
    expect(first.exhausted).toBe(true);
    expect(feedAfterTailRead(emptyFeed, [ev('b'), ev('a')], 2, null).exhausted).toBe(false);
  });

  it('starts from a feed with nothing to page back to', () => {
    expect(emptyFeed.events).toEqual([]);
    expect(emptyFeed.exhausted).toBe(true);
  });

  it('DROPS a page whose cursor is no longer the feed it was asked of', () => {
    // The interleaving: 加载更早 requested the rows below 'b', and while it was in
    // flight a mutation refresh found the feed gapped and replaced it. Appending
    // here would put ['b','a'] straight under ['z','y'] and lose the middle for
    // good — the next 加载更早 carries on below 'a' and never comes back for it.
    const replaced = onScreen([ev('z'), ev('y')], false);
    expect(feedAfterTailRead(replaced, [ev('b'), ev('a')], 20, 'b')).toBe(replaced);
  });

  it('drops both halves, not just the rows', () => {
    // A page that may not speak for the rows may not speak for 「nothing older」:
    // a short stale page setting exhaustion would hide 加载更早 over the very rows
    // the replacement still needs it to reach.
    const replaced = onScreen([ev('z')], false);
    expect(feedAfterTailRead(replaced, [], 20, 'b').exhausted).toBe(false);
  });

  it('drops a mount page that lands after rows are already on screen', () => {
    // Same rule, the other cursor: `null` means 「from the top」, and a feed with a
    // tail is not the feed that question was about.
    const held = onScreen([ev('c'), ev('b')], false);
    expect(feedAfterTailRead(held, [ev('q')], 20, null)).toBe(held);
  });

  it('accepts a page whose cursor still holds after the feed grew at the HEAD', () => {
    // Growing at the head does not move the tail, so the page still attaches — the
    // ordinary case, and the reason this checks the cursor rather than identity.
    const grown = onScreen([ev('d'), ev('c'), ev('b')], false);
    expect(ids(feedAfterTailRead(grown, [ev('a')], 20, 'b').events)).toEqual(['d', 'c', 'b', 'a']);
  });
});

describe('the page reads the feed through this owner', () => {
  const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');
  const firstPaint = readFileSync(join(__dirname, 'firstPaintRegions.ts'), 'utf8');
  const mutationSettlement = readFileSync(join(__dirname, 'mutationSettlement.ts'), 'utf8');

  it('re-reads events in the shared post-write refresh', () => {
    // The whole point: the refresh every mutation site already calls is what has
    // to move the feed, not a probe-only twin bolted onto one call site.
    expect(page).toMatch(/const refresh = React\.useCallback[\s\S]*?void refreshEventHead\(\)[\s\S]*?refreshAuthority\.run/);
    expect(page).toMatch(/eventReadAuthority\.run[\s\S]*?modelsApi\.listEvents\(EVENT_PAGE\)/);
    expect(page).toMatch(/setEventsRead[\s\S]*?foldRegionRead<ResolutionEvent\[\],[\s\S]*?\(incoming,[\s\S]*?feedAfterHeadRead\(previousFeed, freshEvents\)/);
    expect(page).toMatch(/feedAfterTailRead\(emptyFeed, freshEvents, EVENT_PAGE, null\)/);
  });

  it('lets the ancillary feed read fail without losing the rows', () => {
    // A slow or broken /events must not enter the operational first-paint barrier.
    const landing = mutationSettlement.slice(
      mutationSettlement.indexOf('export const readSurfaceLanding'),
      mutationSettlement.indexOf('\n\nexport type SourceMutationLanding ='),
    );
    expect(landing).not.toMatch(/listEvents|events/);
    expect(page).toMatch(/createLatestAsyncAuthority<RegionRead<ResolutionEvent\[\]>>/);
    expect(page).toMatch(/createLatestAsyncAuthority<AuthorizedSurfaceLanding>/);
    expect(page).toMatch(/sourceEntityAuthority\.beginSnapshot\(\)/);
    expect(page).toMatch(/foldRegionRead<Source\[\],[\s\S]*?\(landing\.sources,[\s\S]*?sourceEntityAuthority\.settleSnapshot\(sourceSnapshot, freshSources\)/);
    // Region reads also settle independently: a failed source list cannot erase
    // a successful backend projection, and vice versa.
    expect(firstPaint).toMatch(/\[K in keyof FirstPaintRegionValues\]: RegionRead<FirstPaintRegionValues\[K\]>/);
    expect(firstPaint).toMatch(/satisfies Record<keyof FirstPaintRegionValues, string>/);
  });

  it('moves rows and end-of-feed together, through the owners', () => {
    // One state, so no transition can move the rows and leave the flag behind.
    expect(page).toMatch(/const \[eventsRead, setEventsRead\] = React\.useState<RegionRead<EventFeed>>/);
    expect(page).toMatch(/feedAfterTailRead\(emptyFeed, freshEvents, EVENT_PAGE, null\)/);
    expect(page).toMatch(/setEventsRead\(\(previous\) => readyRegion\(feedAfterTailRead\(foldRegionRead\(previous,[\s\S]*?events, EVENT_PAGE, cursor\)\)\)/);
    expect(page).not.toMatch(/\bregionData\s*\(/);
    // Nothing left that could set one half on its own.
    expect(page).not.toMatch(/setEventsExhausted|setEvents\(/);
    // And no hand-rolled dedupe or page-length test outside the owners.
    expect(page).not.toMatch(/new Set\(prev\.map/);
    expect(page).not.toMatch(/\.length < EVENT_PAGE/);
  });

  it('reads 加载更早 off the same feed it renders', () => {
    expect(page).toMatch(/events=\{eventsRead\}/);
    expect(page).toMatch(/sources=\{sourcesRead\}/);
    // Through the owner, and the SAME cursor is handed back to the merge — the
    // request and the answer have to be about one row for the check to mean
    // anything, which a second hand-rolled read of the tail would not guarantee.
    expect(page).toMatch(/const cursor = feedTailCursor\(feed\)/);
    expect(page).toMatch(/modelsApi\.listEvents\(EVENT_PAGE, cursor\)/);
    expect(page).not.toMatch(/feed\.events\[feed\.events\.length - 1\]/);
  });
});
