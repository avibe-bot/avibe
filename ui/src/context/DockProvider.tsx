import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useApi } from './ApiContext';
import { DockContext, type DockValue } from './DockContext';
import {
  BUILTIN_DOCK_IDS,
  reconcileDock,
  seedDefaultDock,
  showDockId,
  type DockDoc,
} from './dockDoc';
import { useLatestRef } from '@/lib/useLatestRef';

// Fetches the Dock document once, keeps it reconciled against the apps this client
// knows (see dockDoc.ts for the shape and the reconcile rules), and exposes optimistic
// install (pin) / uninstall (unpin) / dock / undock / reorder actions that roll back if
// the server rejects the write.
// Every-built-in-docked default so the Dock renders its resident tiles
// immediately (no flicker) before the server document loads. Matches the
// server's fresh-instance seed; reconcileDock takes over once the GET resolves.
const DEFAULT_DOC: DockDoc = seedDefaultDock();
const DISABLED_DOCK_VALUE: DockValue = {
  order: DEFAULT_DOC.order,
  pins: [],
  isPinned: () => false,
  isDocked: (dockId) => DEFAULT_DOC.order.includes(dockId),
  pinFor: () => null,
  pin: () => Promise.resolve(),
  unpin: () => Promise.resolve(),
  dock: () => Promise.resolve(),
  undock: () => Promise.resolve(),
  setOrder: () => Promise.resolve(),
};

export const DockProvider: React.FC<{ children: React.ReactNode; enabled?: boolean }> = ({
  children,
  enabled = true,
}) => {
  if (!enabled) {
    return (
      <DockContext.Provider value={DISABLED_DOCK_VALUE}>{children}</DockContext.Provider>
    );
  }
  return <EnabledDockProvider>{children}</EnabledDockProvider>;
};

const EnabledDockProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const api = useApi();
  const [doc, setDoc] = useState<DockDoc>(DEFAULT_DOC);
  // Latest committed doc for the async actions' rollback (avoids stale closures).
  const docRef = useLatestRef(doc);
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
    (order: string[]): Promise<void> => {
      // Send the client's baseline id set (built-ins ∪ installed pins) so the
      // server can reject a STALE reorder: because omission now means "undock", a
      // tab that hasn't seen a pin another tab just installed would otherwise
      // silently undock it. On rejection runMutation re-syncs from the server.
      const known = [...BUILTIN_DOCK_IDS, ...docRef.current.pins.map((p) => showDockId(p.session_id))];
      return runMutation({ order, pins: docRef.current.pins }, () => api.setDockOrder(order, known));
    },
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
