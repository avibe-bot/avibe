import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { APP_LIST } from '../apps/registry';
import { useApi } from './ApiContext';

// The workbench Dock is durable, cross-device *product* state (see
// core/dock_store.py). Two layers (§7.1c): `pins` is the INSTALLED set of AI
// Show Pages (built-ins are implicitly installed); `order` is the DOCKED subset —
// the resident tiles, in user order, a SUBSET of the known ids. This provider
// fetches the document once, keeps it reconciled against the apps the client
// knows, and exposes optimistic install (pin) / uninstall (unpin) /
// dock / undock / reorder actions that roll back if the server rejects the write.
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
const BUILTIN_DOCK_IDS: string[] = APP_LIST.map((app) => app.id);

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

export interface DockValue {
  /** Reconciled docked subset (built-in ids + `show:<id>` pins), in user order. */
  order: string[];
  /** Installed AI pages (built-ins are implicitly installed, not listed here). */
  pins: DockPin[];
  /** Whether a session's Show Page is installed (pinned). */
  isPinned: (sessionId: string) => boolean;
  /** Whether a Dock id (built-in or `show:<id>`) is currently in the Dock. */
  isDocked: (dockId: string) => boolean;
  /** The pin record for a session, or null. */
  pinFor: (sessionId: string) => DockPin | null;
  /** Install a session's Show Page — also docks it (optimistic; idempotent). */
  pin: (sessionId: string) => Promise<void>;
  /** Uninstall a session's Show Page — removes it from install + Dock (optimistic; idempotent). */
  unpin: (sessionId: string) => Promise<void>;
  /** Add a known tile (built-in or installed page) to the Dock (optimistic; idempotent). */
  dock: (dockId: string) => Promise<void>;
  /** Remove a tile from the Dock, keeping it installed (optimistic; idempotent). */
  undock: (dockId: string) => Promise<void>;
  /** Persist a new resident-tile order (optimistic; rolls back if rejected). */
  setOrder: (order: string[]) => Promise<void>;
}

const DockContext = createContext<DockValue | null>(null);

// Every-built-in-docked default so the Dock renders its resident tiles
// immediately (no flicker) before the server document loads. Matches the
// server's fresh-instance seed; reconcileDock takes over once the GET resolves.
const DEFAULT_DOC: DockDoc = seedDefaultDock();

export const DockProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const api = useApi();
  const [doc, setDoc] = useState<DockDoc>(DEFAULT_DOC);
  // Latest committed doc for the async actions' rollback (avoids stale closures).
  const docRef = useRef(doc);
  docRef.current = doc;
  // Dock writes are serialized. Each mutation shows its optimistic doc at once
  // (responsiveness), then queues the server request so requests run in action
  // order — never overlapping. A monotonic counter marks the latest mutation and
  // only its response is applied; because the queue runs requests sequentially,
  // that latest response already reflects every earlier write, so the UI
  // converges to the server state instead of dropping a superseded-but-successful
  // pin (Codex). The same counter guards the one-time initial load, so a slow GET
  // can't clobber a just-pinned page.
  const mutationSeqRef = useRef(0);
  const queueRef = useRef<Promise<unknown>>(Promise.resolve());

  const apply = useCallback((next: DockDoc) => setDoc(reconcileDock(next)), []);

  const runMutation = useCallback(
    (optimistic: DockDoc, request: () => Promise<{ ok?: boolean; dock?: DockDoc } | undefined>): Promise<void> => {
      const seq = (mutationSeqRef.current += 1);
      apply(optimistic);
      const task = async () => {
        try {
          const res = await request();
          if (mutationSeqRef.current !== seq) return; // superseded → the newer mutation is authoritative
          if (res?.dock && res.ok !== false) {
            setDoc(reconcileDock(res.dock)); // success → adopt the server doc
            return;
          }
          // else: server rejected (ok:false, e.g. a stale order) → fall through to re-sync
        } catch {
          if (mutationSeqRef.current !== seq) return; // superseded
          // network error → fall through to re-sync
        }
        // The latest mutation failed. Re-sync the authoritative doc from the
        // server rather than rolling back to a captured `prev`: an earlier
        // superseded failure may have been baked into this optimistic state, so a
        // `prev` rollback could re-introduce a phantom tile (Codex). Still
        // seq-guarded so a newer mutation still wins.
        try {
          const fresh = await api.getDock();
          if (mutationSeqRef.current === seq && fresh?.dock) setDoc(reconcileDock(fresh.dock));
        } catch {
          // Offline: best-effort; the next successful load or mutation reconciles.
        }
      };
      // Chain regardless of the previous task's outcome so one failure can't stall the queue.
      const next = queueRef.current.then(task, task);
      queueRef.current = next;
      return next;
    },
    [api, apply],
  );

  useEffect(() => {
    let cancelled = false;
    const loadSeq = mutationSeqRef.current;
    api
      .getDock()
      .then((res) => {
        // Drop the initial snapshot if a mutation started before it resolved, so a
        // slow GET can't clobber a just-pinned page (Codex).
        if (cancelled || mutationSeqRef.current !== loadSeq) return;
        if (res?.dock) setDoc(reconcileDock(res.dock));
      })
      // A failed load (offline / auth) leaves the builtins-only default in place.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [api]);

  const pin = useCallback(
    (sessionId: string): Promise<void> => {
      const prev = docRef.current;
      if (prev.pins.some((p) => p.session_id === sessionId)) return Promise.resolve(); // already pinned
      return runMutation(
        {
          order: [...prev.order, showDockId(sessionId)],
          pins: [...prev.pins, { session_id: sessionId, title_snapshot: '', pinned_at: '' }],
        },
        () => api.pinDockShowPage(sessionId),
      );
    },
    [api, runMutation],
  );

  const unpin = useCallback(
    (sessionId: string): Promise<void> =>
      runMutation(
        {
          order: docRef.current.order.filter((id) => id !== showDockId(sessionId)),
          pins: docRef.current.pins.filter((p) => p.session_id !== sessionId),
        },
        () => api.unpinDockShowPage(sessionId),
      ),
    [api, runMutation],
  );

  const setOrder = useCallback(
    (order: string[]): Promise<void> =>
      runMutation({ order, pins: docRef.current.pins }, () => api.setDockOrder(order)),
    [api, runMutation],
  );

  // Dock / undock a KNOWN tile (built-in or installed page) by editing the order
  // subset — install membership (pins) is untouched, so undocking keeps the page
  // installed. Both go through setOrder (PUT order), reusing its optimistic +
  // rollback path; idempotent so a redundant toggle makes no request.
  const dock = useCallback(
    (dockId: string): Promise<void> => {
      const cur = docRef.current.order;
      if (cur.includes(dockId)) return Promise.resolve();
      return setOrder([...cur, dockId]);
    },
    [setOrder],
  );

  const undock = useCallback(
    (dockId: string): Promise<void> => {
      const cur = docRef.current.order;
      if (!cur.includes(dockId)) return Promise.resolve();
      return setOrder(cur.filter((id) => id !== dockId));
    },
    [setOrder],
  );

  const pinnedSessions = useMemo(() => new Set(doc.pins.map((p) => p.session_id)), [doc.pins]);
  const dockedSet = useMemo(() => new Set(doc.order), [doc.order]);

  const value = useMemo<DockValue>(
    () => ({
      order: doc.order,
      pins: doc.pins,
      isPinned: (sessionId: string) => pinnedSessions.has(sessionId),
      isDocked: (dockId: string) => dockedSet.has(dockId),
      pinFor: (sessionId: string) => doc.pins.find((p) => p.session_id === sessionId) ?? null,
      pin,
      unpin,
      dock,
      undock,
      setOrder,
    }),
    [doc, pinnedSessions, dockedSet, pin, unpin, dock, undock, setOrder],
  );

  return <DockContext.Provider value={value}>{children}</DockContext.Provider>;
};

export function useDock(): DockValue {
  const ctx = useContext(DockContext);
  if (!ctx) throw new Error('useDock must be used within a DockProvider');
  return ctx;
}
