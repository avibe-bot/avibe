// The feed merge, in both directions.
//
// The regression these pin: 最近切换 was fetched once at mount, so a write that
// filed an event (a failing 试跑 cooling its own head down) changed the source row
// while the line explaining that change stayed invisible until a full reload. The
// post-write refresh now re-reads the head — which is only safe if it cannot throw
// away the rows 加载更早 已经 paged in, and cannot splice a hole into the sequence.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { feedAfterHeadRead, headReadGapped, mergeEventFeed } from './eventFeed';
import type { ResolutionEvent } from './types';

/** A feed row: only `id` matters to the merge, and only identity to the rest. */
const ev = (id: string): ResolutionEvent =>
  ({ id, ts: '2026-07-30T10:00:00Z', agent: 'claude', kind: 'cooldown' }) as ResolutionEvent;

const ids = (events: ResolutionEvent[]) => events.map((e) => e.id);

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
    const onScreen = [ev('c'), ev('b'), ev('a')];
    expect(ids(feedAfterHeadRead(onScreen, [ev('d'), ev('c')]))).toEqual(['d', 'c', 'b', 'a']);
  });

  it('replaces rather than splices when the pages no longer touch', () => {
    // The paged rows are still reachable by paging; a spliced feed is reachable by
    // nothing, because no row in it looks wrong.
    expect(ids(feedAfterHeadRead([ev('c'), ev('b')], [ev('z'), ev('y')]))).toEqual(['z', 'y']);
  });

  it('holds the screen when the head read came back empty', () => {
    expect(ids(feedAfterHeadRead([ev('b'), ev('a')], []))).toEqual(['b', 'a']);
  });
});

describe('the page reads the feed through this owner', () => {
  const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');

  it('re-reads events in the shared post-write refresh', () => {
    // The whole point: the refresh every mutation site already calls is what has
    // to move the feed, not a probe-only twin bolted onto one call site.
    expect(page).toMatch(/refreshAuthority\.run\([\s\S]*?modelsApi\.listEvents\(EVENT_PAGE\)/);
    expect(page).toMatch(/setEvents\(\(prev\) => feedAfterHeadRead\(prev, headEvents\)\)/);
  });

  it('merges both directions through one function', () => {
    expect(page).toMatch(/setEvents\(\(prev\) => mergeEventFeed\(prev, page\)\)/);
    // No hand-rolled dedupe left behind to drift from the owner.
    expect(page).not.toMatch(/new Set\(prev\.map/);
  });

  it('leaves tail exhaustion out of a head read', () => {
    // `eventsExhausted` is a fact about the far end; a head page cannot speak for
    // it, and recomputing it there would strand 加载更早.
    const start = page.indexOf('createLatestAsyncAuthority<');
    // Bounded by the authority's own landing callback: the mount read that follows
    // it DOES set exhaustion, legitimately, from a first page that is the tail too.
    const lander = page.slice(start, page.indexOf('React.useEffect(', start));
    expect(lander).toContain('feedAfterHeadRead');
    expect(lander).not.toContain('setEventsExhausted');
  });
});
