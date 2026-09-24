declare global {
  interface Window {
    /** Defined by the Avibe desktop shell before any page script runs, top-level document only. */
    readonly __AVIBE_DESKTOP_SHELL__?: true;
  }
}

/**
 * True when this document is the Avibe desktop shell's own window rather than a
 * browser tab or an installed PWA. This is the host, not the viewport size: see
 * `useIsDesktop` for the layout breakpoint.
 */
export function isDesktopShell(): boolean {
  return typeof window !== 'undefined' && window.__AVIBE_DESKTOP_SHELL__ === true;
}
