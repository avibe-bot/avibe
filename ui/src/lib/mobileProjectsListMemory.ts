export const MOBILE_SESSION_PAGE_SIZE = 8;
export const APP_SHELL_SCROLL_ID = 'app-shell-scroll';

export type MobileProjectsVisibleCounts = Record<string, number>;

export type MobileProjectsListSnapshot = {
  visibleCounts: MobileProjectsVisibleCounts;
  scrollTop: number;
};

const EMPTY_SNAPSHOT: MobileProjectsListSnapshot = { visibleCounts: {}, scrollTop: 0 };

let snapshot: MobileProjectsListSnapshot = EMPTY_SNAPSHOT;
let heldForChatReturn = false;

// The scroll position also outlives the document, in this tab only
// (sessionStorage), because the list can come back in a new one: a tab the
// browser discarded in the background reloads the conversation, and going back
// then loads the list afresh. Only the position: how far each project was
// revealed belongs to the document that revealed it.
const SCROLL_KEY = 'avibe.projects-list.scroll.v1';
// Whether this document has a list position of its own yet. Until it does, the
// one a previous document of this tab stored is the one to restore.
let rememberedInDocument = false;
// The stored position this document inherited: undefined until read, null once
// there is none or it has been restored.
let inheritedScrollTop: number | null | undefined;

function storeScrollTop(top: number): void {
  rememberedInDocument = true;
  try {
    window.sessionStorage.setItem(SCROLL_KEY, String(top));
  } catch {
    // Without storage a new document simply opens the list at the top.
  }
}

function forgetStoredScrollTop(): void {
  rememberedInDocument = true;
  inheritedScrollTop = null;
  try {
    window.sessionStorage.removeItem(SCROLL_KEY);
  } catch {
    // Nothing stored to forget.
  }
}

/** The position a previous document of this tab left the list at, when this
 *  document has none of its own, until ``consumeInheritedProjectsListScroll``. */
export function inheritedProjectsListScroll(): number | null {
  if (inheritedScrollTop === undefined) {
    inheritedScrollTop = null;
    if (!rememberedInDocument) {
      try {
        const top = Number(window.sessionStorage.getItem(SCROLL_KEY) ?? Number.NaN);
        if (Number.isFinite(top) && top > 0) inheritedScrollTop = top;
      } catch {
        // No storage, nothing inherited.
      }
    }
  }
  return inheritedScrollTop;
}

/** The inherited position was restored, or given up: it is not restored again. */
export function consumeInheritedProjectsListScroll(): void {
  inheritedScrollTop = null;
}

export function isChatRoutePath(pathname: string): boolean {
  return pathname.startsWith('/chat/');
}

export function isProjectsRoutePath(pathname: string): boolean {
  return pathname === '/projects';
}

export function visibleSessionCountFor(
  counts: MobileProjectsVisibleCounts,
  projectId: string,
  pageSize: number = MOBILE_SESSION_PAGE_SIZE,
): number {
  return counts[projectId] ?? pageSize;
}

export function revealMoreVisibleCount(
  currentCount: number,
  pageSize: number = MOBILE_SESSION_PAGE_SIZE,
): number {
  return currentCount + pageSize;
}

export function clearProjectVisibleCount(
  counts: MobileProjectsVisibleCounts,
  projectId: string,
): MobileProjectsVisibleCounts {
  if (!(projectId in counts)) return counts;
  const next = { ...counts };
  delete next[projectId];
  return next;
}

export function readMobileProjectsListSnapshot(): MobileProjectsListSnapshot {
  return snapshot;
}

export function writeMobileProjectsListSnapshot(next: MobileProjectsListSnapshot): void {
  snapshot = {
    visibleCounts: { ...next.visibleCounts },
    scrollTop: Number.isFinite(next.scrollTop) ? Math.max(0, next.scrollTop) : 0,
  };
  storeScrollTop(snapshot.scrollTop);
}

export function holdMobileProjectsListForChatReturn(next: MobileProjectsListSnapshot): void {
  writeMobileProjectsListSnapshot(next);
  heldForChatReturn = true;
}

export function markMobileProjectsListRestored(): void {
  heldForChatReturn = false;
}

export function rememberMobileProjectsListOnPageLeave(current: MobileProjectsListSnapshot): void {
  if (heldForChatReturn) return;
  writeMobileProjectsListSnapshot(current);
}

export function rememberMobileProjectsListScroll(scrollTop: number): void {
  if (heldForChatReturn) return;
  snapshot = {
    ...snapshot,
    scrollTop: Number.isFinite(scrollTop) ? Math.max(0, scrollTop) : 0,
  };
  storeScrollTop(snapshot.scrollTop);
}

export function rememberMobileProjectsListCounts(visibleCounts: MobileProjectsVisibleCounts): void {
  if (heldForChatReturn) return;
  snapshot = {
    ...snapshot,
    visibleCounts: { ...visibleCounts },
  };
}

export function forgetMobileProjectsListUnlessPreserved(pathname: string): void {
  // Chat is a detail of this list: keep the revealed window. Any other route
  // is a real departure, even if the user reached it through chat.
  if (isProjectsRoutePath(pathname) || isChatRoutePath(pathname)) return;
  snapshot = EMPTY_SNAPSHOT;
  heldForChatReturn = false;
  forgetStoredScrollTop();
}

export function isMobileProjectsListHeldForChatReturn(): boolean {
  return heldForChatReturn;
}

export function clearMobileProjectsListSnapshot(): void {
  snapshot = EMPTY_SNAPSHOT;
  heldForChatReturn = false;
  forgetStoredScrollTop();
}

type ScrollOwner = {
  scrollTop: number;
  scrollHeight?: number;
  clientHeight?: number;
  addEventListener?(type: 'scroll', listener: () => void, options?: { passive?: boolean }): void;
  removeEventListener?(type: 'scroll', listener: () => void): void;
};

export function readAppShellScrollTop(el: ScrollOwner | null | undefined): number {
  const top = el?.scrollTop;
  return typeof top === 'number' && Number.isFinite(top) ? Math.max(0, top) : 0;
}

export function writeAppShellScrollTop(el: ScrollOwner | null | undefined, top: number): void {
  if (!el) return;
  el.scrollTop = Number.isFinite(top) ? Math.max(0, top) : 0;
}

export function appShellScrollElement(
  root: { getElementById(id: string): ScrollOwner | null } | null = typeof document === 'undefined' ? null : document,
): ScrollOwner | null {
  return root?.getElementById(APP_SHELL_SCROLL_ID) ?? null;
}
