import { useCallback, useLayoutEffect, useRef, type RefObject } from 'react';
import { INBOX_REVERT_AFTER_MS, type InboxFilter } from '../../lib/inboxFilterMemory';
import { APP_SHELL_SCROLL_ID, writeAppShellScrollTop } from '../../lib/mobileProjectsListMemory';

type Snapshot = {
  entryKey: string;
  filter: InboxFilter;
  savedAt: number;
  scrollTop: number;
  anchors: { id: string; top: number }[];
};

// One Inbox -> Chat return, tied to its history entry, not every future visit.
// Keep only geometry and IDs; the Inbox provider already owns the loaded pages.
let saved: Snapshot | null = null;

function scrollOwner(root: HTMLElement): HTMLElement | null {
  const shell = root.closest<HTMLElement>(`#${APP_SHELL_SCROLL_ID}`);
  return shell && getComputedStyle(shell).overflowY !== 'visible'
    ? shell
    : root.ownerDocument.scrollingElement as HTMLElement | null;
}

function viewportTop(owner: HTMLElement): number {
  return owner === owner.ownerDocument.scrollingElement ? 0 : owner.getBoundingClientRect().top;
}

export function useInboxScrollRestoration(
  rootRef: RefObject<HTMLDivElement | null>,
  entryKey: string,
  filter: InboxFilter,
  visible: readonly { session_id: string }[],
) {
  const pending = useRef(saved);

  useLayoutEffect(() => {
    const snapshot = pending.current;
    if (!snapshot) return;
    if (snapshot.entryKey !== entryKey || snapshot.filter !== filter
      || Date.now() - snapshot.savedAt > INBOX_REVERT_AFTER_MS) {
      pending.current = null;
      return;
    }
    const root = rootRef.current;
    const owner = root && scrollOwner(root);
    if (!root || !owner) return;

    const restore = () => {
      if (pending.current !== snapshot) return;
      const rows = new Map(Array.from(root.querySelectorAll<HTMLElement>('[data-inbox-session-id]'))
        .map((row) => [row.dataset.inboxSessionId, row]));
      // A returning route may paint before its cached/reconciled rows arrive.
      // Do not consume the position while the content is still empty.
      if (rows.size === 0) return;
      const anchor = snapshot.anchors.find(({ id }) => rows.has(id));
      const row = anchor && rows.get(anchor.id);
      const top = snapshot.scrollTop === 0 ? 0 : row && anchor
        ? owner.scrollTop + row.getBoundingClientRect().top - viewportTop(owner) - anchor.top
        : 0;
      writeAppShellScrollTop(owner, top);
    };

    restore();
    const frame = requestAnimationFrame(restore);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(restore);
    observer?.observe(root);
    observer?.observe(owner);
    const stop = () => {
      pending.current = null;
      observer?.disconnect();
      cancelAnimationFrame(frame);
    };
    const inputs = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const;
    inputs.forEach((event) => owner.addEventListener(event, stop, { passive: true, capture: true }));
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
      inputs.forEach((event) => owner.removeEventListener(event, stop, true));
    };
  }, [entryKey, filter, rootRef, visible]);

  return useCallback(() => {
    const root = rootRef.current;
    const owner = root && scrollOwner(root);
    if (!root || !owner) return;
    const top = viewportTop(owner);
    const height = owner.clientHeight;
    const candidates = Array.from(root.querySelectorAll<HTMLElement>('[data-inbox-session-id]')).map((row) => {
      const rect = row.getBoundingClientRect();
      const offset = rect.top - top;
      const bottom = rect.bottom - top;
      return {
        id: row.dataset.inboxSessionId!,
        top: bottom > 0 && offset < height
          ? offset
          : Math.max(0, Math.min(offset, height - rect.height)),
        distance: bottom <= 0 ? -bottom : offset >= height ? offset - height : 0,
      };
    });
    // Prefer visible rows, then the nearest surviving neighbor if read/archive
    // updates remove every visible row while Chat is open.
    candidates.sort((a, b) => a.distance - b.distance);
    saved = {
      entryKey,
      filter,
      savedAt: Date.now(),
      scrollTop: owner.scrollTop,
      anchors: candidates.map(({ id, top: offset }) => ({ id, top: offset })),
    };
    pending.current = null;
  }, [entryKey, filter, rootRef]);
}
