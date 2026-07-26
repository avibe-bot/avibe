// Remembers the run-graph "show disabled triggers" legend toggle across visits.
// Disabled trigger chips are hidden by default (contract A11); this persists the
// user's opt-in so a reload doesn't silently re-hide the definitions they wanted
// to see. Mirrors the localStorage conventions used elsewhere in the UI
// (module-level key, SSR-safe, best-effort try/catch — see inboxFilterMemory).
const SHOW_DISABLED_KEY = 'vibe-remote:graph-show-disabled';

export function readGraphShowDisabled(): boolean {
  try {
    return window.localStorage.getItem(SHOW_DISABLED_KEY) === '1';
  } catch {
    // Best-effort persistence only (private mode / SSR / blocked storage).
    return false;
  }
}

export function writeGraphShowDisabled(value: boolean): void {
  try {
    window.localStorage.setItem(SHOW_DISABLED_KEY, value ? '1' : '0');
  } catch {
    // Best-effort persistence only.
  }
}
