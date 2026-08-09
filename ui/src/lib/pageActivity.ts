export type PageActivitySnapshot = {
  visibilityState: DocumentVisibilityState;
  hasFocus: boolean;
};

/** A page is actively presented only while its document is visible and focused. */
export function isPageActive(snapshot: PageActivitySnapshot): boolean {
  return snapshot.visibilityState === 'visible' && snapshot.hasFocus;
}

export function canMarkConversationRead({
  pageActive,
  sessionReady,
  viewResolved,
  historicalWindow,
  showPageActive,
  foregroundAppWindow,
}: {
  pageActive: boolean;
  sessionReady: boolean;
  viewResolved: boolean;
  historicalWindow: boolean;
  showPageActive: boolean;
  foregroundAppWindow: boolean;
}): boolean {
  return (
    pageActive &&
    sessionReady &&
    viewResolved &&
    !historicalWindow &&
    !showPageActive &&
    !foregroundAppWindow
  );
}

export function readPageActivity(): boolean {
  if (typeof document === 'undefined') return false;
  return isPageActive({
    visibilityState: document.visibilityState,
    hasFocus: typeof document.hasFocus !== 'function' || document.hasFocus(),
  });
}
