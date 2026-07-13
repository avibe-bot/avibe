import { describe, expect, it } from 'vitest';

import {
  dockIdToSession,
  MAX_PINNED_PAGES,
  reconcileDock,
  seedDefaultDock,
  showDockId,
  type DockDoc,
} from './DockContext';

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

describe('seedDefaultDock', () => {
  it('docks every built-in in canonical order, nothing installed', () => {
    const out = seedDefaultDock(BUILTINS);
    expect(out.order).toEqual(BUILTINS);
    expect(out.pins).toEqual([]);
  });
});

describe('reconcileDock (two-layer subset model)', () => {
  it('keeps an empty order empty — never force-seeds built-ins', () => {
    // The empty Dock is valid: reconcile honors it (only the pre-load default,
    // seedDefaultDock, docks built-ins).
    const out = reconcileDock({ order: [], pins: [] }, BUILTINS);
    expect(out.order).toEqual([]);
    expect(out.pins).toEqual([]);
  });

  it('tolerates null / undefined input (empty subset)', () => {
    expect(reconcileDock(null, BUILTINS).order).toEqual([]);
    expect(reconcileDock(undefined, BUILTINS).order).toEqual([]);
  });

  it('does NOT re-add a built-in the stored order omits (undocked stays undocked)', () => {
    // 'editor' absent from the stored order → it stays out of the Dock.
    const out = reconcileDock({ order: ['terminal', 'files'], pins: [] }, BUILTINS);
    expect(out.order).toEqual(['terminal', 'files']);
    expect(out.order).not.toContain('editor');
  });

  it('does NOT dock an installed pin that is absent from order', () => {
    // The page is installed (in pins) but undocked (not in order) → order unchanged.
    const doc: DockDoc = {
      order: ['files', 'terminal', 'editor'],
      pins: [{ session_id: 'ses_a', title_snapshot: 'A', pinned_at: 't' }],
    };
    const out = reconcileDock(doc, BUILTINS);
    expect(out.order).toEqual(['files', 'terminal', 'editor']);
    expect(out.order).not.toContain('show:ses_a');
    expect(out.pins).toHaveLength(1); // still installed
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
      order: ['show:ses_a'],
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

  it('clamps oversized pins to the fixed pin budget', () => {
    // Far more pins than the budget allows; only the first MAX_PINNED_PAGES survive.
    // The budget is FIXED (independent of the built-in count), so adding a built-in
    // never shrinks it — a valid pre-Phase-2 dock keeps all its pins. The order is
    // honored as stored (empty here — nothing docked), never force-populated.
    const pins = Array.from({ length: 250 }, (_, i) => ({ session_id: `ses_${i}`, title_snapshot: '', pinned_at: '' }));
    const out = reconcileDock({ order: [], pins }, BUILTINS);
    expect(out.pins).toHaveLength(MAX_PINNED_PAGES); // 197, regardless of built-in count
    expect(out.order).toEqual([]); // stored empty order honored
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
