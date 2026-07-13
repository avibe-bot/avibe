// Global terminal font-size preference.
//
// One value shared by every open terminal — the /apps/terminal route, every
// windowed terminal, and every tab within them — so a zoom applies everywhere at
// once instead of per-instance. Persisted to localStorage under a versioned key so
// the choice survives reloads and new tabs open at the same size.
//
// Kept as a tiny module-level store with a subscriber set (mirrors terminalSlots.ts)
// because terminals don't share a common React parent: the route terminal and each
// windowed terminal mount independently, so a change from one must reach the others
// through a shared store, not props. All window/localStorage access is guarded so the
// module is safe to import in a non-DOM (test) environment.
const STORAGE_KEY = 'avibe.terminal.fontSize.v1';

export const TERMINAL_FONT_MIN = 9;
export const TERMINAL_FONT_MAX = 24;
export const TERMINAL_FONT_DEFAULT = 13;

const clamp = (n: number): number =>
  Math.min(TERMINAL_FONT_MAX, Math.max(TERMINAL_FONT_MIN, Math.round(n)));

function read(): number {
  try {
    if (typeof window === 'undefined') return TERMINAL_FONT_DEFAULT;
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw == null || raw === '') return TERMINAL_FONT_DEFAULT;
    const n = Number(raw);
    return Number.isFinite(n) ? clamp(n) : TERMINAL_FONT_DEFAULT;
  } catch {
    // Storage blocked (private mode, disabled cookies) — fall back to the default.
    return TERMINAL_FONT_DEFAULT;
  }
}

let current = read();
const listeners = new Set<(size: number) => void>();

export function getTerminalFontSize(): number {
  return current;
}

function set(size: number): void {
  const next = clamp(size);
  if (next === current) return; // already there (e.g. ⌘+ at the max) — no refit, no churn
  current = next;
  try {
    if (typeof window !== 'undefined') window.localStorage.setItem(STORAGE_KEY, String(next));
  } catch {
    // Persistence is best-effort; keep the in-memory value even if the write fails.
  }
  for (const listener of listeners) listener(next);
}

/** Nudge the shared size by `delta` steps (clamped to [MIN, MAX]); notifies every terminal. */
export function adjustTerminalFontSize(delta: number): void {
  set(current + delta);
}

/** Restore the default size (⌘0). */
export function resetTerminalFontSize(): void {
  set(TERMINAL_FONT_DEFAULT);
}

/** Subscribe to size changes; returns an unsubscribe fn. Does not fire on subscribe. */
export function subscribeTerminalFontSize(listener: (size: number) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// Test-only: reset the shared preference between cases.
export function _resetTerminalFontSize(): void {
  current = TERMINAL_FONT_DEFAULT;
  listeners.clear();
}
