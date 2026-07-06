import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { APP_REGISTRY, type AppId } from '../apps/registry';
import {
  WINDOW_RESTORE_PARAM,
  loadWorkbenchWindows,
  saveWorkbenchWindows,
  stripRestoreParam,
  type PersistedWindow,
} from '../lib/workbenchPersistence';

// One open app window. Bounds are in CSS px relative to the window LAYER (the
// workbench main area, right of the sidebar). z drives stacking + focus order.
export interface WindowBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface WindowInstance {
  id: string;
  appId: AppId;
  /** Optional per-instance title override (e.g. an open file path); falls back to the app's titleKey. */
  title?: string;
  /** Per-instance launch params surfaced to the app body (e.g. the file an Editor window opens). */
  params?: Record<string, unknown>;
  bounds: WindowBounds;
  z: number;
  minimized: boolean;
  maximized: boolean;
  // Bounds captured before a maximize, restored on un-maximize.
  restoreBounds?: WindowBounds;
}

export interface OpenAppOptions {
  title?: string;
  bounds?: Partial<WindowBounds>;
  params?: Record<string, unknown>;
}

export interface WindowManagerValue {
  windows: WindowInstance[];
  /** The current top window id (highest z, not minimized), or null. */
  focusedId: string | null;
  openApp: (appId: AppId, opts?: OpenAppOptions) => string;
  close: (id: string) => void;
  focus: (id: string) => void;
  minimize: (id: string) => void;
  /** Un-minimize and bring to front. */
  restore: (id: string) => void;
  toggleMaximize: (id: string) => void;
  /** Patch a window's bounds (used by drag + resize). */
  setBounds: (id: string, bounds: Partial<WindowBounds>) => void;
  /** Set (or clear) a window's title — e.g. the Editor reflecting its active file so several open
   *  windows are distinguishable in the Dock + titlebar. No-op when the title is unchanged. */
  setTitle: (id: string, title: string | undefined) => void;
  /**
   * Register (or clear, by passing null) a guard a window body uses to veto closing.
   * The getter returns a confirm message when closing would lose work, else null.
   */
  setCloseGuard: (id: string, getMessage: (() => string | null) | null) => void;
  /**
   * Register (or clear, by passing null) a provider a window body uses to contribute its own
   * JSON-able state to the persisted layout (e.g. the Editor's open tabs, the Terminal's tab
   * titles). Read on save; the value comes back through the window's params on the next restore.
   */
  setStateProvider: (id: string, getState: (() => unknown) | null) => void;
  /**
   * Mark a window as closing (its exit animation has started but it's still mounted until
   * animationEnd calls close()). Excludes it from the persisted snapshot so a reload during the
   * ~300ms close animation doesn't write back — and resurrect — a window the user just closed.
   */
  markClosing: (id: string) => void;
  /** Run a window's close guard (confirm if it has a message); true = may close. */
  confirmClose: (id: string) => boolean;
}

const WindowManagerContext = createContext<WindowManagerValue | null>(null);

const DEFAULT_SIZE = { width: 760, height: 520 };
// Cascade each new window down-right from the last so stacks stay reachable.
const CASCADE_STEP = 32;
const CASCADE_WRAP = 6;

// Persistence is desktop-only: the window layer is `hidden md:block` and windowed apps open only
// from the desktop-only launcher/Dock, so restoring windows below md would mount invisible bodies
// and a `[]` save from a phone would clobber a real desktop layout. Read live (not frozen at mount)
// so a desktop tab that starts narrow and is widened still restores/saves once it crosses md.
function isDesktopViewport(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(min-width: 768px)').matches
    : false;
}

export const WindowManagerProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const idSeq = useRef(0);
  const zSeq = useRef(0);
  const openCount = useRef(0);
  // Whether the persisted layout has been restored yet — set the first time we're on desktop, so a
  // desktop-at-mount restore and a restore-after-widen never both fire.
  const restoredRef = useRef(false);

  // Load the persisted layout and seed the id/z/open sequences past the restored maxima (idempotent)
  // so new windows don't collide with, or open behind, restored ones.
  const loadAndSeed = (): WindowInstance[] => {
    const restored = loadWorkbenchWindows();
    for (const w of restored) {
      const n = Number.parseInt(w.id.slice(4), 10); // strip the validated "win-" prefix
      if (Number.isFinite(n)) idSeq.current = Math.max(idSeq.current, n);
      zSeq.current = Math.max(zSeq.current, w.z);
    }
    openCount.current = Math.max(openCount.current, restored.length);
    return restored;
  };

  // Restore in the lazy initializer when the shell mounts at desktop width, so `windows` never
  // starts empty there (which would let the first debounced save wipe the stored layout). A shell
  // that mounts narrow restores later, on crossing the breakpoint (see the effect below).
  const [windows, setWindows] = useState<WindowInstance[]>(() => {
    if (!isDesktopViewport()) return [];
    restoredRef.current = true;
    return loadAndSeed();
  });

  // Latest windows for the debounced/pagehide save closures (which fire after render).
  const windowsRef = useRef(windows);
  windowsRef.current = windows;

  // Per-window close guards: a body (e.g. a dirty editor) registers a getter that
  // returns a confirm message when closing would lose work. Held in a ref so it
  // never triggers re-renders.
  const closeGuards = useRef(new Map<string, () => string | null>());
  // Per-window state providers: a body registers a getter returning its JSON-able snapshot
  // (Editor tabs, Terminal tab titles), read when the layout is saved. Same ref pattern.
  const stateProviders = useRef(new Map<string, () => unknown>());
  // Windows whose close exit animation has started but which are still mounted (removed only at
  // animationEnd). Excluded from the snapshot so a reload mid-animation doesn't re-persist them.
  const closingIds = useRef(new Set<string>());

  const setCloseGuard = useCallback((id: string, getMessage: (() => string | null) | null) => {
    if (getMessage) closeGuards.current.set(id, getMessage);
    else closeGuards.current.delete(id);
  }, []);

  const setStateProvider = useCallback((id: string, getState: (() => unknown) | null) => {
    if (getState) stateProviders.current.set(id, getState);
    else stateProviders.current.delete(id);
  }, []);

  const markClosing = useCallback((id: string) => {
    closingIds.current.add(id);
  }, []);

  // Snapshot every window plus its body's state (from its provider). A provider that throws
  // contributes no appState rather than failing the whole save. Drop the injected restore payload
  // from params so a re-save keeps only original launch params (no nested stale snapshots).
  const buildSnapshot = useCallback((): PersistedWindow[] =>
    windowsRef.current
      .filter((w) => !closingIds.current.has(w.id))
      .map((w) => {
        let appState: unknown;
        try {
          appState = stateProviders.current.get(w.id)?.();
        } catch {
          appState = undefined;
        }
        // A restored window's lazy body may not have mounted + registered its provider yet. Until
        // it does, carry the payload it was restored WITH (still in params) so a save during that
        // gap doesn't drop the very state we're persisting for.
        if (appState === undefined) appState = w.params?.[WINDOW_RESTORE_PARAM];
        return { ...w, params: stripRestoreParam(w.params), appState };
      }), []);

  // Debounced persistence: coalesce bursts of geometry/z/open-close changes into one write.
  const saveTimer = useRef<number | null>(null);
  const flushSave = useCallback(() => {
    if (saveTimer.current !== null) {
      clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    if (!isDesktopViewport()) return; // never persist (incl. clobber with []) from a non-desktop viewport
    saveWorkbenchWindows(buildSnapshot());
  }, [buildSnapshot]);
  const scheduleSave = useCallback(() => {
    if (!isDesktopViewport()) return;
    if (saveTimer.current !== null) clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null;
      saveWorkbenchWindows(buildSnapshot());
    }, 400);
  }, [buildSnapshot]);

  const confirmClose = useCallback((id: string): boolean => {
    const message = closeGuards.current.get(id)?.();
    return !message || window.confirm(message);
  }, []);

  const focus = useCallback((id: string) => {
    setWindows((prev) => {
      const target = prev.find((w) => w.id === id);
      if (!target) return prev;
      const nextZ = ++zSeq.current;
      // Already on top → no churn.
      if (target.z === nextZ - 1 && prev.every((w) => w.id === id || w.z < target.z)) return prev;
      return prev.map((w) => (w.id === id ? { ...w, z: nextZ } : w));
    });
  }, []);

  const openApp = useCallback<WindowManagerValue['openApp']>((appId, opts) => {
    const def = APP_REGISTRY[appId];
    const size = { ...DEFAULT_SIZE, ...def?.defaultSize };
    const i = openCount.current++ % CASCADE_WRAP;
    const id = `win-${++idSeq.current}`;
    const z = ++zSeq.current;
    const bounds: WindowBounds = {
      // Cascade starts clear of the 240px sidebar so a new window doesn't open over it
      // (the layer now spans the full viewport); it can still be dragged/maximized over it.
      x: 264 + i * CASCADE_STEP,
      y: 32 + i * CASCADE_STEP,
      width: size.width,
      height: size.height,
      ...opts?.bounds,
    };
    setWindows((prev) => [
      ...prev,
      {
        id,
        appId,
        title: opts?.title,
        params: opts?.params,
        bounds,
        z,
        minimized: false,
        maximized: false,
      },
    ]);
    return id;
  }, []);

  const close = useCallback((id: string) => {
    closeGuards.current.delete(id);
    stateProviders.current.delete(id);
    closingIds.current.delete(id);
    setWindows((prev) => prev.filter((w) => w.id !== id));
  }, []);

  const minimize = useCallback((id: string) => {
    setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, minimized: true } : w)));
  }, []);

  const restore = useCallback(
    (id: string) => {
      setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, minimized: false } : w)));
      focus(id);
    },
    [focus],
  );

  const toggleMaximize = useCallback((id: string) => {
    setWindows((prev) =>
      prev.map((w) => {
        if (w.id !== id) return w;
        if (w.maximized) {
          return { ...w, maximized: false, bounds: w.restoreBounds ?? w.bounds, restoreBounds: undefined };
        }
        return { ...w, maximized: true, restoreBounds: w.bounds };
      }),
    );
    focus(id);
  }, [focus]);

  const setBounds = useCallback((id: string, patch: Partial<WindowBounds>) => {
    setWindows((prev) => prev.map((w) => (w.id === id ? { ...w, bounds: { ...w.bounds, ...patch } } : w)));
  }, []);

  const setTitle = useCallback((id: string, title: string | undefined) => {
    // Return the SAME array reference when nothing changes so a body that calls this every render
    // (the Editor, on active-tab change) can't trigger a re-render loop.
    setWindows((prev) => {
      const w = prev.find((x) => x.id === id);
      if (!w || w.title === title) return prev;
      return prev.map((x) => (x.id === id ? { ...x, title } : x));
    });
  }, []);

  // If the shell mounted below md (initializer skipped restore), restore the saved layout the first
  // time the viewport crosses to desktop — BEFORE the now-enabled saves could persist an empty list
  // over it. The window list is still empty while narrow (windowed apps open only from the
  // desktop-only launcher), so replacing it here can't discard anything the user opened.
  useEffect(() => {
    if (restoredRef.current) return;
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mql = window.matchMedia('(min-width: 768px)');
    const restoreOnce = () => {
      if (restoredRef.current || !mql.matches) return;
      restoredRef.current = true;
      const restored = loadAndSeed();
      setWindows((cur) => (cur.length ? cur : restored));
    };
    restoreOnce(); // may have crossed to desktop between the initial render and this effect
    mql.addEventListener('change', restoreOnce);
    return () => mql.removeEventListener('change', restoreOnce);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Persist on any window-list change (open/close, geometry, z, min/max, title). Body-only state
  // (Editor tabs, Terminal titles) that doesn't touch the window list is captured by the same
  // provider read on the next save this triggers, and — authoritatively — by the pagehide flush.
  // scheduleSave itself no-ops below md, so this stays inert until the viewport is desktop-sized.
  useEffect(() => {
    scheduleSave();
  }, [windows, scheduleSave]);

  // A reload fires pagehide (not React unmount): flush a final synchronous snapshot so the very
  // latest body state (a just-renamed terminal tab, a just-opened editor file) is captured.
  // flushSave no-ops below md, so no phone/narrow viewport ever writes.
  useEffect(() => {
    window.addEventListener('pagehide', flushSave);
    return () => {
      window.removeEventListener('pagehide', flushSave);
      if (saveTimer.current !== null) clearTimeout(saveTimer.current);
    };
  }, [flushSave]);

  const focusedId = useMemo(() => {
    const visible = windows.filter((w) => !w.minimized);
    if (visible.length === 0) return null;
    return visible.reduce((top, w) => (w.z > top.z ? w : top)).id;
  }, [windows]);

  const value = useMemo<WindowManagerValue>(
    () => ({
      windows,
      focusedId,
      openApp,
      close,
      focus,
      minimize,
      restore,
      toggleMaximize,
      setBounds,
      setTitle,
      setCloseGuard,
      setStateProvider,
      markClosing,
      confirmClose,
    }),
    [windows, focusedId, openApp, close, focus, minimize, restore, toggleMaximize, setBounds, setTitle, setCloseGuard, setStateProvider, markClosing, confirmClose],
  );

  return <WindowManagerContext.Provider value={value}>{children}</WindowManagerContext.Provider>;
};

export function useWindowManager(): WindowManagerValue {
  const ctx = useContext(WindowManagerContext);
  if (!ctx) throw new Error('useWindowManager must be used within a WindowManagerProvider');
  return ctx;
}

// A window body calls this to veto its own close while there's unsaved work: pass
// the owning window id and a confirm message (or null when clean). No-ops for a
// non-windowed (full-page) mount, where windowId is undefined.
export function useWindowCloseGuard(windowId: string | undefined, message: string | null): void {
  const { setCloseGuard } = useWindowManager();
  const messageRef = useRef(message);
  messageRef.current = message;
  useEffect(() => {
    if (!windowId) return;
    setCloseGuard(windowId, () => messageRef.current);
    return () => setCloseGuard(windowId, null);
  }, [windowId, setCloseGuard]);
}

// A window body calls this to contribute its own JSON-able state to the persisted layout, so a
// reload can restore it (Editor open tabs, Terminal tab titles). `getState` may be a fresh closure
// each render — it's held in a ref and read lazily at save time, so it always sees current state
// without re-registering. No-ops for a non-windowed (full-page) mount, where windowId is undefined.
export function useWindowState(windowId: string | undefined, getState: () => unknown): void {
  const { setStateProvider } = useWindowManager();
  const getStateRef = useRef(getState);
  getStateRef.current = getState;
  useEffect(() => {
    if (!windowId) return;
    setStateProvider(windowId, () => getStateRef.current());
    return () => setStateProvider(windowId, null);
  }, [windowId, setStateProvider]);
}
