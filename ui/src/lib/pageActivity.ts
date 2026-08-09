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
  historicalWindow,
  showPageActive,
}: {
  pageActive: boolean;
  historicalWindow: boolean;
  showPageActive: boolean;
}): boolean {
  return pageActive && !historicalWindow && !showPageActive;
}

export function readPageActivity(): boolean {
  if (typeof document === 'undefined') return false;
  return isPageActive({
    visibilityState: document.visibilityState,
    hasFocus: typeof document.hasFocus !== 'function' || document.hasFocus(),
  });
}
