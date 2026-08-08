import { describe, expect, it } from 'vitest';

import type { WorkbenchMessage } from '../context/ApiContext';
import {
  byCreatedThenId,
  isTranscriptWindowDisjoint,
  mergeAnchorWindow,
  mergeById,
  insertMessageOrdered,
  messageOrderTimeMs,
  transcriptWindowsOverlap,
  timestampOrderTimeMs,
  transcriptOrderTimeMs,
} from './transcriptOrder';

// The transcript keeps rows in durable transcript-entry order. Direct Messages
// use created_at plus their time-sortable id; accepted Delivery Messages use
// delivered_at so time spent waiting in the queue cannot move them above the
// reply that completed first.
const t = (s: number) => `2024-01-01T00:00:${String(s).padStart(2, '0')}Z`;
const mk = (id: string, created_at: string, delivered_at: string | null = null): WorkbenchMessage =>
  ({ id, created_at, delivered_at }) as unknown as WorkbenchMessage;
const ids = (list: WorkbenchMessage[]): string[] => list.map((m) => m.id);

describe('messageOrderTimeMs', () => {
  it('recovers subsecond positions from canonical message ids', () => {
    const createdAt = '2026-07-30T10:00:00Z';
    const preciseTime = Date.parse('2026-07-30T10:00:00.800Z');
    const preciseId = `msg_${Math.floor(preciseTime * 1_000).toString(16).padStart(15, '0')}deadbeef`;

    expect(messageOrderTimeMs(mk(preciseId, createdAt))).toBe(preciseTime);
    expect(messageOrderTimeMs(mk('imported-id', createdAt))).toBe(Date.parse(createdAt));
  });
});

describe('transcriptOrderTimeMs', () => {
  it('uses native acceptance for a message submitted while another turn was active', () => {
    expect(
      transcriptOrderTimeMs(
        mk('msg_000000000000001deadbeef', '2026-08-04T00:00:01Z', '2026-08-04T00:00:03.500000Z'),
      ),
    ).toBeCloseTo(Date.parse('2026-08-04T00:00:03.500Z'), 6);
  });
});

describe('timestampOrderTimeMs', () => {
  it('retains microseconds from UTC and offset ISO timestamps', () => {
    const base = Date.parse('2026-07-30T10:00:00.500Z');

    expect(timestampOrderTimeMs('2026-07-30T10:00:00.500100Z')).toBeCloseTo(base + 0.1, 6);
    expect(timestampOrderTimeMs('2026-07-30T18:00:00.500900+08:00')).toBeCloseTo(base + 0.9, 6);
  });

  it('preserves Date.parse invalid timestamp behavior', () => {
    expect(timestampOrderTimeMs('not-a-timestamp')).toBeNaN();
  });
});

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

  it('places a queued input after the reply that completed before its acceptance', () => {
    const reply = mk('msg_003', '2026-08-04T00:00:02Z');
    const queued = mk('msg_002', '2026-08-04T00:00:01Z', '2026-08-04T00:00:03.500000Z');
    expect(byCreatedThenId(reply, queued)).toBe(-1);
  });
});

describe('isTranscriptWindowDisjoint', () => {
  it('uses acceptance order rather than sortable ids at the reconciliation boundary', () => {
    const previousNewest = mk('msg_003', '2026-08-04T00:00:02Z');
    const tailOldest = mk(
      'msg_002',
      '2026-08-04T00:00:01Z',
      '2026-08-04T00:00:03.500000Z',
    );

    expect(isTranscriptWindowDisjoint(previousNewest, tailOldest)).toBe(true);
  });
});

describe('transcriptWindowsOverlap', () => {
  it('detects a shared row between fetched windows', () => {
    expect(transcriptWindowsOverlap([mk('a', t(1)), mk('b', t(2))], [mk('b', t(2)), mk('c', t(3))])).toBe(true);
    expect(transcriptWindowsOverlap([mk('a', t(1))], [mk('b', t(2))])).toBe(false);
    expect(transcriptWindowsOverlap([], [mk('b', t(2))])).toBe(false);
  });
});

describe('mergeAnchorWindow', () => {
  it('keeps the owning reply when a following-tail trim would drop it', () => {
    const existing = Array.from({ length: 4 }, (_, index) => mk(`tail-${index}`, t(index + 4)));
    const incoming = [mk('owner', t(1)), mk('tail-0', t(4))];

    expect(mergeAnchorWindow(existing, incoming, 'owner', 4, true)).toEqual({
      messages: incoming,
      replaced: true,
      detachedTail: false,
      trimmedOldest: false,
    });
  });

  it('keeps the owning reply when trimming from the newest side', () => {
    const existing = Array.from({ length: 4 }, (_, index) => mk(`head-${index}`, t(index)));
    const incoming = [mk('head-3', t(3)), mk('owner', t(4))];

    expect(mergeAnchorWindow(existing, incoming, 'owner', 4, false)).toEqual({
      messages: incoming,
      replaced: true,
      detachedTail: true,
      trimmedOldest: false,
    });
  });

  it('marks a newest-side trim historical even when the anchor is retained', () => {
    const existing = Array.from({ length: 4 }, (_, index) => mk(`head-${index}`, t(index)));
    const incoming = [mk('head-0', t(0)), mk('new-tail', t(4))];

    expect(mergeAnchorWindow(existing, incoming, 'head-0', 4, false)).toEqual({
      messages: existing,
      replaced: false,
      detachedTail: true,
      trimmedOldest: false,
    });
  });

  it('reports when a following-tail trim discards older rows', () => {
    const existing = Array.from({ length: 4 }, (_, index) => mk(`tail-${index}`, t(index + 4)));
    const incoming = [mk('old', t(1)), mk('tail-0', t(4))];

    expect(mergeAnchorWindow(existing, incoming, 'tail-0', 4, true)).toEqual({
      messages: existing,
      replaced: false,
      detachedTail: false,
      trimmedOldest: true,
    });
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
      id: 'm1',
      created_at: t(1),
      source_session_id: 'ses_src',
      source_session_title: 'Src',
      source_session_agent_name: 'pm',
    } as unknown as WorkbenchMessage;
    const [row] = mergeById([live], [enriched]);
    expect(row.source_session_id).toBe('ses_src');
    expect(row.source_session_title).toBe('Src');
    expect(row.source_session_agent_name).toBe('pm');
  });

  it('fills trigger provenance recovered from a legacy native id', () => {
    const live = {
      id: 'm1',
      created_at: t(1),
      delivered_at: null,
      author_name: null,
      author_id: null,
    } as unknown as WorkbenchMessage;
    const enriched = {
      id: 'm1',
      created_at: t(1),
      delivered_at: null,
      author_name: 'watch',
      author_id: 'def_watch',
    } as unknown as WorkbenchMessage;
    const [row] = mergeById([live], [enriched]);
    expect(row.author_name).toBe('watch');
    expect(row.author_id).toBe('def_watch');
  });

  it('merges Vault provenance metadata into an existing live row', () => {
    const live = {
      id: 'm1',
      created_at: t(1),
      metadata: { source_kind: 'callback', source_actor: 'vault:vrq_1' },
    } as unknown as WorkbenchMessage;
    const enriched = {
      id: 'm1',
      created_at: t(1),
      metadata: {
        source_kind: 'callback',
        source_actor: 'vault:vrq_1',
        vault_request_type: 'access',
        vault_request_status: 'denied',
      },
    } as unknown as WorkbenchMessage;

    const [row] = mergeById([live], [enriched]);
    expect(row.metadata).toEqual(enriched.metadata);
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
