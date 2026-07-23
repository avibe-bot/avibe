import { describe, expect, it } from 'vitest';

import type { WorkbenchMessage } from '../context/ApiContext';
import { byCreatedThenId, mergeById, insertMessageOrdered } from './transcriptOrder';

// The transcript keeps its rows in durable (created_at, id) order at all times.
// Only id + created_at drive ordering, so fixtures fill just those; the rest is
// cast away. ``created_at`` is second-resolution server-side, so same-second rows
// with different ids exercise the id tie-break (the case that would otherwise let
// a fast agent result render ahead of its prompt). Timestamps stay whole-second to
// match the real format — mixing fractional seconds would only test a string-
// compare artifact the server never produces.
const t = (s: number) => `2024-01-01T00:00:${String(s).padStart(2, '0')}Z`;
const mk = (id: string, created_at: string): WorkbenchMessage =>
  ({ id, created_at }) as unknown as WorkbenchMessage;
const ids = (list: WorkbenchMessage[]): string[] => list.map((m) => m.id);

describe('byCreatedThenId', () => {
  it('orders by created_at first', () => {
    expect(byCreatedThenId(mk('b', t(1)), mk('a', t(2)))).toBe(-1);
    expect(byCreatedThenId(mk('a', t(2)), mk('b', t(1)))).toBe(1);
  });

  it('breaks created_at ties by id', () => {
    expect(byCreatedThenId(mk('a', t(1)), mk('b', t(1)))).toBe(-1);
    expect(byCreatedThenId(mk('b', t(1)), mk('a', t(1)))).toBe(1);
  });

  it('returns 0 only for the same id at the same time', () => {
    expect(byCreatedThenId(mk('a', t(1)), mk('a', t(1)))).toBe(0);
  });
});

describe('mergeById', () => {
  it('dedupes by id and sorts into durable order', () => {
    const existing = [mk('p1', t(1))];
    const incoming = [mk('p1', t(1)), mk('r1', t(2))];
    expect(ids(mergeById(existing, incoming))).toEqual(['p1', 'r1']);
  });

  it('places an out-of-order result behind its prompt (same second, id tie-break)', () => {
    // The result row (id ``r``) arrived over the stream BEFORE its prompt (id
    // ``p``), both stamped the same second. Durable order must keep p before r.
    const existing = [mk('r', t(5))];
    expect(ids(mergeById(existing, [mk('p', t(5))]))).toEqual(['p', 'r']);
  });

  it('handles empty inputs', () => {
    expect(mergeById([], [])).toEqual([]);
    expect(ids(mergeById([], [mk('a', t(1))]))).toEqual(['a']);
  });

  it('fills late-arriving source-session provenance onto an existing live row (A9a)', () => {
    const live = { id: 'm1', created_at: t(1), source_session_id: null } as unknown as WorkbenchMessage;
    const enriched = {
      id: 'm1', created_at: t(1),
      source_session_id: 'ses_src', source_session_title: 'Src', source_session_agent_name: 'pm',
    } as unknown as WorkbenchMessage;
    const [row] = mergeById([live], [enriched]);
    expect(row.source_session_id).toBe('ses_src');
    expect(row.source_session_title).toBe('Src');
    expect(row.source_session_agent_name).toBe('pm');
  });

  it('does not overwrite an already-resolved source-session id with a null reconcile', () => {
    const existing = { id: 'm1', created_at: t(1), source_session_id: 'ses_src' } as unknown as WorkbenchMessage;
    const incoming = { id: 'm1', created_at: t(1), source_session_id: null } as unknown as WorkbenchMessage;
    expect(mergeById([existing], [incoming])[0].source_session_id).toBe('ses_src');
  });
});

describe('insertMessageOrdered', () => {
  // Gaps between seconds leave room to insert a strictly-in-between row.
  const base = () => [mk('a', t(1)), mk('c', t(3)), mk('e', t(5))];

  it('returns [msg] for an empty transcript', () => {
    expect(ids(insertMessageOrdered([], mk('a', t(1))))).toEqual(['a']);
  });

  it('appends (fast path) a message newer than the tail without re-sorting', () => {
    expect(ids(insertMessageOrdered(base(), mk('z', t(7))))).toEqual(['a', 'c', 'e', 'z']);
  });

  it('returns the SAME array reference on a duplicate id (React skips the render)', () => {
    const list = base();
    expect(insertMessageOrdered(list, mk('c', t(3)))).toBe(list);
  });

  it('binary-inserts an out-of-order arrival at the head', () => {
    expect(ids(insertMessageOrdered(base(), mk('0', t(0))))).toEqual(['0', 'a', 'c', 'e']);
  });

  it('binary-inserts an out-of-order arrival into the middle', () => {
    // Stamped between ``a`` (t1) and ``c`` (t3) → lands at index 1.
    expect(ids(insertMessageOrdered(base(), mk('b', t(2))))).toEqual(['a', 'b', 'c', 'e']);
  });

  it('respects the id tie-break when created_at matches an existing row', () => {
    // Same second as ``c`` (t3): id ``bb`` < ``c`` sorts before it, ``cc`` > ``c`` after.
    expect(ids(insertMessageOrdered(base(), mk('bb', t(3))))).toEqual(['a', 'bb', 'c', 'e']);
    expect(ids(insertMessageOrdered(base(), mk('cc', t(3))))).toEqual(['a', 'c', 'cc', 'e']);
  });

  it('never mutates the input array', () => {
    const list = base();
    insertMessageOrdered(list, mk('0', t(0)));
    expect(ids(list)).toEqual(['a', 'c', 'e']);
  });

  it('matches mergeById ordering for any single-row insert (equivalence)', () => {
    const list = base();
    for (const probe of [mk('0', t(0)), mk('b', t(2)), mk('z', t(7)), mk('cc', t(3))]) {
      expect(ids(insertMessageOrdered(list, probe))).toEqual(ids(mergeById(list, [probe])));
    }
  });
});
