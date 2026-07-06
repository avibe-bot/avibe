// Versioned localStorage persistence for the desktop window layer.
//
// The WindowManager holds every open app window in memory (see WindowManagerContext),
// so a browser reload used to lose the whole workbench: which windows were open, their
// geometry / z-order / min-max state, the Editor's folder + file tabs, and the Terminal's
// tab layout. This module serializes that layout to localStorage under a versioned key and
// rebuilds it on the next mount. SPA navigation already survives (the provider lives in the
// AppShell layout route); this covers a real page reload.
//
// Only the geometry lives here directly. Each window body (the Editor, the Terminal) captures
// its own JSON-able snapshot via a state provider (see useWindowState) into `appState`; on
// restore that blob is handed back to the body through its launch params under
// WINDOW_RESTORE_PARAM, so this layer never has to know what any app stores.

import { APP_REGISTRY } from '../apps/registry';
import type { WindowBounds, WindowInstance } from '../context/WindowManagerContext';

// Bump the version suffix to invalidate an incompatible on-disk shape — a stored value under
// an older key is simply never read, so old data is ignored silently (not migrated).
export const WORKBENCH_WINDOWS_STORAGE_KEY = 'avibe.workbench.windows.v1';

// The launch-param key under which a restored window's captured body state is handed back to
// its app body (alongside any original launch params). The Editor + Terminal read it on mount
// to re-open their tabs; apps without persisted state ignore it.
export const WINDOW_RESTORE_PARAM = '__avibeRestoredState';

// Sanity caps against a corrupt/oversized stored value — well above any real workbench, but bounded
// so one bad localStorage entry can't spawn thousands of windows / tabs / shells and freeze the UI.
export const MAX_RESTORED_WINDOWS = 40;
export const MAX_RESTORED_TABS = 50;

// One window as persisted. Mirrors the rehydratable subset of WindowInstance, plus the body's
// own snapshot in `appState`. This is the on-disk schema — keep it explicit and stable; change
// the storage-key version if it changes incompatibly.
export interface PersistedWindow {
  id: string;
  appId: string;
  title?: string;
  params?: Record<string, unknown>;
  bounds: WindowBounds;
  z: number;
  minimized: boolean;
  maximized: boolean;
  restoreBounds?: WindowBounds;
  appState?: unknown;
}

interface PersistedPayload {
  version: 1;
  windows: PersistedWindow[];
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function isBounds(v: unknown): v is WindowBounds {
  if (!v || typeof v !== 'object') return false;
  const b = v as Record<string, unknown>;
  return isFiniteNumber(b.x) && isFiniteNumber(b.y) && isFiniteNumber(b.width) && isFiniteNumber(b.height);
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

// A stored window is usable only if every rehydratable field is well-formed AND its app is still
// registered — a window whose app was removed (or a corrupt entry) is dropped, not restored blank.
function toRuntimeWindow(v: unknown): WindowInstance | null {
  if (!isPlainObject(v)) return null;
  const { id, appId, title, params, bounds, z, minimized, maximized, restoreBounds, appState } = v;
  if (typeof id !== 'string') return null;
  // OWN keys only: `in` would accept inherited object keys ("toString", "__proto__") from a corrupt
  // blob, and APP_REGISTRY[thatKey] would then resolve to a non-app value that crashes the layer.
  if (typeof appId !== 'string' || !Object.prototype.hasOwnProperty.call(APP_REGISTRY, appId)) return null;
  if (!isBounds(bounds) || !isFiniteNumber(z) || typeof minimized !== 'boolean' || typeof maximized !== 'boolean') {
    return null;
  }
  if (restoreBounds !== undefined && !isBounds(restoreBounds)) return null;

  const launchParams = isPlainObject(params) ? params : undefined;
  // Hand the body its captured state back through params, so restore reuses the exact same
  // launch path an app already reads (Editor/Terminal check WINDOW_RESTORE_PARAM on mount).
  const nextParams =
    appState !== undefined ? { ...(launchParams ?? {}), [WINDOW_RESTORE_PARAM]: appState } : launchParams;

  // Rebuild explicitly (never spread the raw stored object) so a corrupt/hostile blob can't
  // smuggle unexpected keys onto the runtime window.
  return {
    id,
    appId: appId as WindowInstance['appId'],
    ...(typeof title === 'string' ? { title } : {}),
    ...(nextParams ? { params: nextParams } : {}),
    bounds: { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height },
    z,
    minimized,
    maximized,
    ...(restoreBounds ? { restoreBounds: { x: restoreBounds.x, y: restoreBounds.y, width: restoreBounds.width, height: restoreBounds.height } } : {}),
  };
}

// Parse a raw stored payload into runtime windows. Pure (no storage access) so it's unit-testable;
// any corruption — invalid JSON, wrong/absent version, non-array, bad entries — yields [] or drops
// just the bad entries, never throws.
export function parseWorkbenchWindows(raw: string | null | undefined): WindowInstance[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!isPlainObject(parsed) || parsed.version !== 1 || !Array.isArray(parsed.windows)) return [];
  const windows: WindowInstance[] = [];
  // Bound the scan + output so a corrupt/oversized array can't build thousands of windows.
  for (const entry of parsed.windows.slice(0, MAX_RESTORED_WINDOWS)) {
    const w = toRuntimeWindow(entry);
    if (w) windows.push(w);
  }
  return windows;
}

// Serialize windows to the versioned payload string. Pure; may throw if a body's appState is not
// JSON-serializable (callers wrap this — a bad snapshot must never crash the shell).
export function serializeWorkbenchWindows(windows: PersistedWindow[]): string {
  const payload: PersistedPayload = { version: 1, windows };
  return JSON.stringify(payload);
}

// Remove the injected restore payload from a window's params so a re-save persists only the
// original launch params — the fresh appState replaces it, and re-persisting it would nest a
// stale snapshot inside params on every reload.
export function stripRestoreParam(
  params: Record<string, unknown> | undefined,
): Record<string, unknown> | undefined {
  if (!params || !(WINDOW_RESTORE_PARAM in params)) return params;
  const { [WINDOW_RESTORE_PARAM]: _drop, ...rest } = params;
  return Object.keys(rest).length ? rest : undefined;
}

// Read + parse the persisted layout. Returns [] when storage is unavailable or empty.
export function loadWorkbenchWindows(): WindowInstance[] {
  try {
    return parseWorkbenchWindows(window.localStorage.getItem(WORKBENCH_WINDOWS_STORAGE_KEY));
  } catch {
    return [];
  }
}

// Serialize + write the layout. Silently drops on any failure (storage disabled / quota exceeded /
// a non-serializable snapshot) — persistence is best-effort and must never interrupt the user.
export function saveWorkbenchWindows(windows: PersistedWindow[]): void {
  try {
    window.localStorage.setItem(WORKBENCH_WINDOWS_STORAGE_KEY, serializeWorkbenchWindows(windows));
  } catch {
    // ignore
  }
}
