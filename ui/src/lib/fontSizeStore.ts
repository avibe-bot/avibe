export interface FontSizeStoreOptions {
  min: number;
  max: number;
  default: number;
}

export interface FontSizeStore {
  get: () => number;
  adjust: (delta: number) => void;
  reset: () => void;
  subscribe: (listener: (size: number) => void) => () => void;
  /** Test-only: restore the default and detach subscribers without touching persisted state. */
  _reset: () => void;
}

// Small shared preference store for UI surfaces that mount independently but must follow one
// persisted font size. Browser storage is best-effort so imports and updates remain safe in tests,
// SSR-like environments, and privacy modes where localStorage is unavailable.
export function createFontSizeStore(
  storageKey: string,
  { min, max, default: defaultSize }: FontSizeStoreOptions,
): FontSizeStore {
  const clamp = (n: number): number => Math.min(max, Math.max(min, Math.round(n)));

  const read = (): number => {
    try {
      if (typeof window === 'undefined') return defaultSize;
      const raw = window.localStorage.getItem(storageKey);
      if (raw == null || raw === '') return defaultSize;
      const n = Number(raw);
      return Number.isFinite(n) ? clamp(n) : defaultSize;
    } catch {
      return defaultSize;
    }
  };

  let current = read();
  const listeners = new Set<(size: number) => void>();

  const set = (size: number): void => {
    const next = clamp(size);
    if (next === current) return;
    current = next;
    try {
      if (typeof window !== 'undefined') window.localStorage.setItem(storageKey, String(next));
    } catch {
      // Keep the in-memory preference when persistence is blocked.
    }
    for (const listener of listeners) listener(next);
  };

  return {
    get: () => current,
    adjust: (delta) => set(current + delta),
    reset: () => set(defaultSize),
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    _reset: () => {
      current = defaultSize;
      listeners.clear();
    },
  };
}
