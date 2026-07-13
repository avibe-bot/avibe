import { describe, expect, it } from 'vitest';

import { dockIdToSession, reconcileDock, showDockId, type DockDoc } from './DockContext';

const BUILTINS = ['files', 'terminal', 'editor'];

describe('showDockId / dockIdToSession', () => {
  it('round-trips a session id', () => {
    expect(showDockId('ses_1')).toBe('show:ses_1');
    expect(dockIdToSession('show:ses_1')).toBe('ses_1');
  });

  it('returns null for a non-Show dock id', () => {
    expect(dockIdToSession('files')).toBeNull();
    expect(dockIdToSession('editor')).toBeNull();
  });
});

describe('reconcileDock', () => {
  it('defaults an empty doc to the built-ins in canonical order', () => {
    const out = reconcileDock({ order: [], pins: [] }, BUILTINS);
    expect(out.order).toEqual(['files', 'terminal', 'editor']);
    expect(out.pins).toEqual([]);
  });

  it('tolerates null / undefined input', () => {
    expect(reconcileDock(null, BUILTINS).order).toEqual(BUILTINS);
    expect(reconcileDock(undefined, BUILTINS).order).toEqual(BUILTINS);
  });

  it('appends a missing built-in at the end, preserving the stored order', () => {
    // 'editor' is absent from the stored order → it is appended last.
    const out = reconcileDock({ order: ['terminal', 'files'], pins: [] }, BUILTINS);
    expect(out.order).toEqual(['terminal', 'files', 'editor']);
  });

  it('appends a pinned page that is missing from order', () => {
    const doc: DockDoc = {
      order: ['files', 'terminal', 'editor'],
      pins: [{ session_id: 'ses_a', title_snapshot: 'A', pinned_at: 't' }],
    };
    const out = reconcileDock(doc, BUILTINS);
    expect(out.order).toEqual(['files', 'terminal', 'editor', 'show:ses_a']);
  });

  it('keeps a valid custom order (pin interleaved with built-ins)', () => {
    const doc: DockDoc = {
      order: ['show:ses_a', 'editor', 'files', 'terminal'],
      pins: [{ session_id: 'ses_a', title_snapshot: 'A', pinned_at: 't' }],
    };
    expect(reconcileDock(doc, BUILTINS).order).toEqual(['show:ses_a', 'editor', 'files', 'terminal']);
  });

  it('dedupes pins by session id, keeping the first', () => {
    const doc: DockDoc = {
      order: [],
      pins: [
        { session_id: 'ses_a', title_snapshot: 'first', pinned_at: 't1' },
        { session_id: 'ses_a', title_snapshot: 'second', pinned_at: 't2' },
      ],
    };
    const out = reconcileDock(doc, BUILTINS);
    expect(out.pins).toHaveLength(1);
    expect(out.pins[0].title_snapshot).toBe('first');
    expect(out.order.filter((id) => id === 'show:ses_a')).toHaveLength(1);
  });

  it('drops duplicate ids in order', () => {
    const doc: DockDoc = { order: ['files', 'files', 'terminal', 'editor'], pins: [] };
    expect(reconcileDock(doc, BUILTINS).order).toEqual(['files', 'terminal', 'editor']);
  });

  it('ignores malformed pins without crashing', () => {
    const doc = {
      order: ['files', 'terminal', 'editor'],
      // Missing / non-string session_id, and a non-string snapshot to coerce.
      pins: [{ title_snapshot: 'x' }, { session_id: 42 }, { session_id: 'ses_ok', title_snapshot: null }],
    } as unknown as DockDoc;
    const out = reconcileDock(doc, BUILTINS);
    expect(out.pins).toEqual([{ session_id: 'ses_ok', title_snapshot: '', pinned_at: '' }]);
  });

  // Negative control: an id that is neither a built-in nor a live pin must NOT
  // survive reconciliation — a stale `show:<gone>` or a bogus id is dropped.
  it('drops unknown ids (stale pins and bogus entries)', () => {
    const doc: DockDoc = {
      order: ['files', 'show:ghost', 'bogus', 'terminal', 'editor'],
      pins: [],
    };
    const out = reconcileDock(doc, BUILTINS);
    expect(out.order).not.toContain('show:ghost');
    expect(out.order).not.toContain('bogus');
    expect(out.order).toEqual(['files', 'terminal', 'editor']);
  });

  it('clamps oversized pins to the cap, always keeping the built-ins', () => {
    // Far more pins than the 200 cap allows; only the first (cap - built-ins) survive.
    const pins = Array.from({ length: 250 }, (_, i) => ({ session_id: `ses_${i}`, title_snapshot: '', pinned_at: '' }));
    const out = reconcileDock({ order: [], pins }, BUILTINS);
    expect(out.pins).toHaveLength(200 - BUILTINS.length); // 197
    expect(out.order).toHaveLength(200);
    expect(out.order.slice(0, BUILTINS.length)).toEqual(BUILTINS);
  });

  it('is idempotent', () => {
    const doc: DockDoc = {
      order: ['show:ses_a', 'files'],
      pins: [{ session_id: 'ses_a', title_snapshot: 'A', pinned_at: 't' }],
    };
    const once = reconcileDock(doc, BUILTINS);
    const twice = reconcileDock(once, BUILTINS);
    expect(twice).toEqual(once);
  });
});
