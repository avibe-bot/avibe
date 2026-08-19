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

export function forgetMobileProjectsListUnlessPreserved(pathname: string): void {
  // Chat is a detail of this list: keep the revealed window. Any other route
  // is a real departure, even if the user reached it through chat.
  if (isProjectsRoutePath(pathname) || isChatRoutePath(pathname)) return;
  snapshot = EMPTY_SNAPSHOT;
  heldForChatReturn = false;
}

export function isMobileProjectsListHeldForChatReturn(): boolean {
  return heldForChatReturn;
}

export function clearMobileProjectsListSnapshot(): void {
  snapshot = EMPTY_SNAPSHOT;
  heldForChatReturn = false;
}

type ScrollOwner = { scrollTop: number };

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
