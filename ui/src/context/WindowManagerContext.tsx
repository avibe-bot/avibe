import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { APP_REGISTRY, type AppId } from '../apps/registry';

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
  /** Run a window's close guard (confirm if it has a message); true = may close. */
  confirmClose: (id: string) => boolean;
}

const WindowManagerContext = createContext<WindowManagerValue | null>(null);

const DEFAULT_SIZE = { width: 760, height: 520 };
// Cascade each new window down-right from the last so stacks stay reachable.
const CASCADE_STEP = 32;
const CASCADE_WRAP = 6;

export const WindowManagerProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [windows, setWindows] = useState<WindowInstance[]>([]);
  const idSeq = useRef(0);
  const zSeq = useRef(0);
  const openCount = useRef(0);
  // Per-window close guards: a body (e.g. a dirty editor) registers a getter that
  // returns a confirm message when closing would lose work. Held in a ref so it
  // never triggers re-renders.
  const closeGuards = useRef(new Map<string, () => string | null>());

  const setCloseGuard = useCallback((id: string, getMessage: (() => string | null) | null) => {
    if (getMessage) closeGuards.current.set(id, getMessage);
    else closeGuards.current.delete(id);
  }, []);

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
      confirmClose,
    }),
    [windows, focusedId, openApp, close, focus, minimize, restore, toggleMaximize, setBounds, setTitle, setCloseGuard, confirmClose],
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
