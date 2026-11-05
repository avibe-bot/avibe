import { describe, expect, it } from 'vitest';

import { type CoverageRange, type OrderKey, extendTailCoverage, isCovered, mergePageCoverage } from './ranges';

// What the device may later present as a complete stretch of history rests on
// these rules alone: a range that claims rows it never saw makes an offline
// reader believe nothing is missing.

const key = (n: number): OrderKey => ({ at: `2026-11-05T00:00:${String(n).padStart(2, '0')}.000000Z`, id: `msg_${n}` });
const STREAM = 'doc:1';
const LATER_STREAM = 'doc:2';
const opts = { streamToken: STREAM, now: 1000 };

const tailPage = (first: number, last: number, startReached = false) => ({
  low: startReached ? null : key(first),
  high: null,
  first: key(first),
  last: key(last),
});
const olderPage = (first: number, last: number, startReached = false) => ({
  low: startReached ? null : key(first),
  high: key(last),
  first: key(first),
  last: key(last),
});

describe('local store coverage ranges', () => {
  it('keeps an old tail and a new tail apart after a gap longer than one page', () => {
    // Rows 1-50 were the tail; the stream dropped; 51-120 arrived; the new tail is 71-120.
    const before = mergePageCoverage([], tailPage(1, 50), opts);
    const after = mergePageCoverage(before, tailPage(71, 120), { ...opts, streamToken: LATER_STREAM });

    expect(after).toHaveLength(2);
    expect(isCovered(after, key(60), LATER_STREAM)).toBe(false);
    expect(isCovered(after, key(30), LATER_STREAM)).toBe(true);
    expect(isCovered(after, key(100), LATER_STREAM)).toBe(true);
    // Only the newest tail keeps the tail claim.
    expect(after.filter((range) => range.atTail)).toHaveLength(1);
  });

  it('joins a new tail to the stored tail when the stream stayed continuous', () => {
    const before = mergePageCoverage([], tailPage(1, 50), opts);
    const after = mergePageCoverage(before, tailPage(71, 120), opts);
    expect(after).toHaveLength(1);
    expect(isCovered(after, key(60), STREAM)).toBe(true);
  });

  it('joins an older page only where it shares keys with the range it extends', () => {
    const tail = mergePageCoverage([], tailPage(51, 100), opts);
    // One row of overlap (row 51) proves the join.
    const joined = mergePageCoverage(tail, olderPage(1, 51), opts);
    expect(joined).toHaveLength(1);
    // Ending one row short proves nothing about row 50 to 51.
    const apart = mergePageCoverage(tail, olderPage(1, 50), opts);
    expect(apart).toHaveLength(2);
    expect(isCovered(apart, key(50), STREAM)).toBe(true);
    expect(isCovered(apart, { at: key(50).at, id: 'msg_50z' }, STREAM)).toBe(false);
  });

  it('records the start of history only when the page says nothing is older', () => {
    const withStart = mergePageCoverage([], olderPage(5, 20, true), opts);
    expect(isCovered(withStart, key(1), STREAM)).toBe(true);
    const withoutStart = mergePageCoverage([], olderPage(5, 20), opts);
    expect(isCovered(withoutStart, key(1), STREAM)).toBe(false);
  });

  it('extends a tail with streamed rows only while its stream is the current one', () => {
    const tail = mergePageCoverage([], tailPage(1, 10), opts);
    expect(isCovered(extendTailCoverage(tail, key(11), STREAM), key(11), STREAM)).toBe(true);
    const afterGap = extendTailCoverage(tail, key(11), LATER_STREAM);
    expect(isCovered(afterGap, key(11), LATER_STREAM)).toBe(false);
    expect(afterGap[0].to).toEqual(key(10));
  });

  it('gives a tail claim no reach once its stream is gone', () => {
    const tail: CoverageRange[] = mergePageCoverage([], tailPage(1, 10), opts);
    expect(isCovered(tail, key(30), STREAM)).toBe(true);
    expect(isCovered(tail, key(30), LATER_STREAM)).toBe(false);
  });

  it('treats an empty session answered from both ends as covered while its stream lasts', () => {
    const empty = mergePageCoverage([], { low: null, high: null, first: null, last: null }, opts);
    expect(isCovered(empty, key(1), STREAM)).toBe(true);
    expect(isCovered(empty, key(1), LATER_STREAM)).toBe(false);
  });
});
