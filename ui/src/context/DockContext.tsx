import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { APP_LIST } from '../apps/registry';
import { useApi } from './ApiContext';

// The workbench Dock is durable, cross-device *product* state (see
// core/dock_store.py): which apps sit in the Dock and in what order. This
// provider fetches that document once, keeps it reconciled against the apps the
// client actually knows, and exposes optimistic pin/unpin/reorder actions that
// roll back if the server rejects the write.
//
// A Dock item id is either a built-in app id verbatim (`files` / `terminal` /
// `editor`) or a pinned Show Page as `show:<session_id>`. The built-in id set
// and its canonical order are a contract shared with the backend's
// BUILTIN_DOCK_IDS — both derive from APP_LIST, so keep them in sync.

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
 *   - drop unknown / duplicate ids from `order`;
 *   - append any missing built-ins, then any missing pins, at the end.
 * Pure: no I/O, safe to unit-test and to run on every read.
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

  const pinIds = pins.map((pin) => showDockId(pin.session_id));
  const known = new Set<string>([...builtinIds, ...pinIds]);

  const order: string[] = [];
  const seen = new Set<string>();
  for (const id of doc?.order ?? []) {
    if (known.has(id) && !seen.has(id)) {
      order.push(id);
      seen.add(id);
    }
  }
  for (const id of builtinIds) {
    if (!seen.has(id)) {
      order.push(id);
      seen.add(id);
    }
  }
  for (const id of pinIds) {
    if (!seen.has(id)) {
      order.push(id);
      seen.add(id);
    }
  }
  return { order, pins };
}

export interface DockValue {
  /** Reconciled resident-tile order (built-in ids + `show:<id>` pins). */
  order: string[];
  pins: DockPin[];
  /** Whether a session's Show Page is currently pinned. */
  isPinned: (sessionId: string) => boolean;
  /** The pin record for a session, or null. */
  pinFor: (sessionId: string) => DockPin | null;
  /** Pin a session's Show Page (optimistic; idempotent). */
  pin: (sessionId: string) => Promise<void>;
  /** Unpin a session's Show Page (optimistic; idempotent). */
  unpin: (sessionId: string) => Promise<void>;
  /** Persist a new resident-tile order (optimistic; rolls back if rejected). */
  setOrder: (order: string[]) => Promise<void>;
}

const DockContext = createContext<DockValue | null>(null);

// Builtins-only default so the Dock renders its resident tiles immediately (no
// flicker) before the server document loads.
const DEFAULT_DOC: DockDoc = reconcileDock({ order: [], pins: [] });

export const DockProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const api = useApi();
  const [doc, setDoc] = useState<DockDoc>(DEFAULT_DOC);
  // Latest committed doc for the async actions' rollback (avoids stale closures).
  const docRef = useRef(doc);
  docRef.current = doc;
  // Monotonic mutation counter. A server response (or the one-time initial load)
  // is applied only if no newer local mutation has started since it was
  // dispatched — otherwise an out-of-order response on a slow/remote connection
  // could revert the user's latest pin/unpin/reorder (Codex).
  const mutationSeqRef = useRef(0);

  const apply = useCallback((next: DockDoc) => setDoc(reconcileDock(next)), []);

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
    async (sessionId: string) => {
      const prev = docRef.current;
      if (prev.pins.some((p) => p.session_id === sessionId)) return; // already pinned
      const seq = (mutationSeqRef.current += 1);
      apply({
        order: [...prev.order, showDockId(sessionId)],
        pins: [...prev.pins, { session_id: sessionId, title_snapshot: '', pinned_at: '' }],
      });
      try {
        const res = await api.pinDockShowPage(sessionId);
        if (mutationSeqRef.current !== seq) return; // a newer mutation supersedes this response
        if (res?.dock) setDoc(reconcileDock(res.dock));
      } catch {
        if (mutationSeqRef.current === seq) setDoc(prev); // rollback only if still the latest
      }
    },
    [api, apply],
  );

  const unpin = useCallback(
    async (sessionId: string) => {
      const prev = docRef.current;
      const seq = (mutationSeqRef.current += 1);
      apply({
        order: prev.order.filter((id) => id !== showDockId(sessionId)),
        pins: prev.pins.filter((p) => p.session_id !== sessionId),
      });
      try {
        const res = await api.unpinDockShowPage(sessionId);
        if (mutationSeqRef.current !== seq) return; // a newer mutation supersedes this response
        if (res?.dock) setDoc(reconcileDock(res.dock));
      } catch {
        if (mutationSeqRef.current === seq) setDoc(prev); // rollback only if still the latest
      }
    },
    [api, apply],
  );

  const setOrder = useCallback(
    async (order: string[]) => {
      const prev = docRef.current;
      const seq = (mutationSeqRef.current += 1);
      apply({ order, pins: prev.pins });
      try {
        const res = await api.setDockOrder(order);
        if (mutationSeqRef.current !== seq) return; // a newer mutation supersedes this response
        // The server rejects a stale order (its id set no longer matches) with
        // ok:false; roll back to the last good doc and let the next action /
        // reload reconcile. A thrown error (network) rolls back the same way.
        if (res?.ok && res.dock) setDoc(reconcileDock(res.dock));
        else setDoc(prev);
      } catch {
        if (mutationSeqRef.current === seq) setDoc(prev);
      }
    },
    [api, apply],
  );

  const pinnedSessions = useMemo(() => new Set(doc.pins.map((p) => p.session_id)), [doc.pins]);

  const value = useMemo<DockValue>(
    () => ({
      order: doc.order,
      pins: doc.pins,
      isPinned: (sessionId: string) => pinnedSessions.has(sessionId),
      pinFor: (sessionId: string) => doc.pins.find((p) => p.session_id === sessionId) ?? null,
      pin,
      unpin,
      setOrder,
    }),
    [doc, pinnedSessions, pin, unpin, setOrder],
  );

  return <DockContext.Provider value={value}>{children}</DockContext.Provider>;
};

export function useDock(): DockValue {
  const ctx = useContext(DockContext);
  if (!ctx) throw new Error('useDock must be used within a DockProvider');
  return ctx;
}
