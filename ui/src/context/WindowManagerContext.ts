import { createContext, useContext, useEffect } from 'react';

import type { AppId } from '../apps/registry';
import { useLatestRef } from '@/lib/useLatestRef';

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
  /** The app window that owns foreground focus, or null while the canvas owns it. */
  focusedId: string | null;
  openApp: (appId: AppId, opts?: OpenAppOptions) => string;
  close: (id: string) => void;
  focus: (id: string) => void;
  /** Return foreground focus to the workbench canvas without hiding windows. */
  focusCanvas: () => void;
  minimize: (id: string) => void;
  /** Un-minimize and bring to front. */
  restore: (id: string) => void;
  toggleMaximize: (id: string) => void;
  /** Patch a window's bounds (used by drag + resize). */
  setBounds: (id: string, bounds: Partial<WindowBounds>) => void;
  /** Set (or clear) a window's title — e.g. the Editor reflecting its active file so several open
   *  windows are distinguishable in the Dock + titlebar. No-op when the title is unchanged. */
  setTitle: (id: string, title: string | undefined) => void;
  /** Merge a patch into a window's launch params — lets an external navigation
   *  (e.g. the /admin/show-pages redirect) hand an already-open app a request. */
  setParams: (id: string, params: Record<string, unknown>) => void;
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
  /**
   * True while ANY window is mid drag/resize gesture. Windows shield their body
   * (iframe) with a transparent overlay while it's set, so a gesture whose pointer
   * crosses an iframe can't have its events stolen by the iframe document (§7.1i).
   * Set true at gesture start, false on gesture end (unconditional cleanup).
   */
  gestureActive: boolean;
  setGestureActive: (active: boolean) => void;
}

export const WindowManagerContext = createContext<WindowManagerValue | null>(null);

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
  const messageRef = useLatestRef(message);
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
  const getStateRef = useLatestRef(getState);
  useEffect(() => {
    if (!windowId) return;
    setStateProvider(windowId, () => getStateRef.current());
    return () => setStateProvider(windowId, null);
  }, [windowId, setStateProvider]);
}
