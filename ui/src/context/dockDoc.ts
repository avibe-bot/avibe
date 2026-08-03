import { APP_LIST } from '../apps/registry';

// The shape of the workbench Dock document and the pure rules that canonicalize
// it. `DockContext.tsx` owns the fetch/optimistic-write side; everything here is
// I/O-free so the shared contract stays unit-testable (see dockDoc.test.ts).
//
// The Dock is durable, cross-device *product* state (see core/dock_store.py).
// Two layers (§7.1c): `pins` is the INSTALLED set of AI Show Pages (built-ins are
// implicitly installed); `order` is the DOCKED subset — the resident tiles, in
// user order, a SUBSET of the known ids.
//
// A Dock item id is either a built-in app id verbatim (`files` / `terminal` /
// `editor` / `library`) or a pinned Show Page as `show:<session_id>`. The
// built-in id set and its canonical order are a contract shared with the
// backend's BUILTIN_DOCK_IDS — both derive from APP_LIST, so keep them in sync.
// Any tile, built-ins included, can be undocked (absent from `order`); the empty
// Dock is a valid state.

export type DockPin = {
  session_id: string;
  title_snapshot: string;
  pinned_at: string;
};

export type DockDoc = {
  order: string[];
  pins: DockPin[];
};

export const SHOW_DOCK_PREFIX = 'show:';

// Fixed defensive cap on PINNED Show Pages, mirroring core/dock_store.py's
// MAX_PINNED_PAGES. reconcile clamps pins to this FIXED budget (independent of
// the built-in count) so a corrupt/oversized doc stays bounded AND adding a
// built-in never shrinks the budget or drops an existing valid pin on reconcile.
export const MAX_PINNED_PAGES = 197;

/** The Dock id for a pinned Show Page session. */
export function showDockId(sessionId: string): string {
  return `${SHOW_DOCK_PREFIX}${sessionId}`;
}

/** The session id inside a `show:<id>` Dock id, or null for a non-Show item. */
export function dockIdToSession(dockId: string): string | null {
  return dockId.startsWith(SHOW_DOCK_PREFIX) ? dockId.slice(SHOW_DOCK_PREFIX.length) : null;
}

// The resident built-in tiles, in canonical order. Mirrors the backend
// BUILTIN_DOCK_IDS; `preview` is intentionally absent (opened on demand, never
// resident), exactly like `showpage`.
export const BUILTIN_DOCK_IDS: string[] = APP_LIST.map((app) => app.id);

/**
 * Canonicalize a Dock document against the known built-in ids — the same rule
 * the server applies (core/dock_store._reconcile), so a stale or partial doc
 * from either side converges to one shape:
 *   - dedupe pins by session id (first wins);
 *   - clamp pins to the fixed install budget;
 *   - drop unknown / duplicate ids from `order`.
 * The order is left as the stored SUBSET — built-ins and pins are NOT
 * force-appended (§7.1c), so an undocked tile stays undocked and the empty Dock
 * round-trips. Pure: no I/O, safe to unit-test and to run on every read.
 */
export function reconcileDock(doc: DockDoc | null | undefined, builtinIds: string[] = BUILTIN_DOCK_IDS): DockDoc {
  const pins: DockPin[] = [];
  const seenPins = new Set<string>();
  for (const pin of doc?.pins ?? []) {
    if (!pin || typeof pin.session_id !== 'string' || !pin.session_id || seenPins.has(pin.session_id)) continue;
    seenPins.add(pin.session_id);
    pins.push({
      session_id: pin.session_id,
      title_snapshot: typeof pin.title_snapshot === 'string' ? pin.title_snapshot : '',
      pinned_at: typeof pin.pinned_at === 'string' ? pin.pinned_at : '',
    });
  }

  // Clamp on read (mirrors the backend): built-ins are always kept; excess pins
  // beyond the FIXED pin budget are dropped so a corrupt/oversized doc stays
  // bounded (the budget doesn't shrink when a built-in is added).
  const maxPins = MAX_PINNED_PAGES;
  const clampedPins = pins.length > maxPins ? pins.slice(0, maxPins) : pins;

  const pinIds = clampedPins.map((pin) => showDockId(pin.session_id));
  const known = new Set<string>([...builtinIds, ...pinIds]);

  const order: string[] = [];
  const seen = new Set<string>();
  for (const id of doc?.order ?? []) {
    if (known.has(id) && !seen.has(id)) {
      order.push(id);
      seen.add(id);
    }
  }
  return { order, pins: clampedPins };
}

/**
 * The pre-load default Dock: every built-in docked, nothing installed — matching
 * the server's seed for a fresh instance. Used only as the initial state before
 * the server document loads (avoids a flash of an empty Dock); once the GET
 * resolves, reconcileDock takes over and an undocked built-in stays undocked.
 */
export function seedDefaultDock(builtinIds: string[] = BUILTIN_DOCK_IDS): DockDoc {
  return { order: [...builtinIds], pins: [] };
}
