import type { WindowInstance } from '../../context/WindowManagerContext';

/**
 * Whether the browser's `beforeunload` guard should be armed for the app windows
 * (§7.1g): true iff at least one window is open AND not minimized. Minimized-only
 * windows do not arm it (the PM default) — a minimized window carries no visible,
 * about-to-be-lost surface the user would be surprised to drop on tab-close.
 * Pure — takes only the `minimized` flag, so it is trivial to unit-test.
 */
export function shouldGuardUnload(windows: readonly Pick<WindowInstance, 'minimized'>[]): boolean {
  return windows.some((win) => !win.minimized);
}
