import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';

import { isTouchCapableDevice } from '../../lib/softKeyboard';

// The home's one centered group (ui/design: home/home/z-layout-spec, home/home/z-layout-spec-phone).
// Its blocks keep one order, one width, and one gap; the group sits a little above the middle of
// the page, the free space above and below it split 2 : 3, so a block that comes or goes re-centers
// the whole group. When the page is too short the free space shrinks first (to 24 on a desktop, 8 on
// a phone), then the page scrolls; the gap never shrinks. A large window keeps the 768 group.

/** The page column the shell gives the home: it fills the space beside the sidebar (on a phone,
 *  between the header and the tab bar) and lays the group out in it. */
export function HomeLayout({ children }: { children: ReactNode }) {
  const pageRef = useRef<HTMLDivElement>(null);
  const heldHeight = useKeyboardFreeHeight(pageRef);
  return (
    <div
      ref={pageRef}
      className="flex w-full flex-1 flex-col"
      style={heldHeight === null ? undefined : { minHeight: heldHeight }}
    >
      <div aria-hidden="true" className="min-h-2 shrink grow-2 md:min-h-6" />
      <div className="mx-auto flex w-full max-w-3xl shrink-0 flex-col gap-4 md:gap-7">
        {children}
      </div>
      <div aria-hidden="true" className="min-h-2 shrink grow-3 md:min-h-6" />
    </div>
  );
}

const isEditable = (target: EventTarget | null): boolean =>
  target instanceof HTMLElement
  && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);

/**
 * The soft keyboard must not re-center the group (ui/design: home/home/z-keyboard-phone). iOS pans
 * the locked shell and never resizes it, but Android's `resizes-content` (index.html) shrinks the
 * shell, and the group would re-center in the shorter area. So while a field on the page has focus
 * on a touch device, the page keeps the height it had when the field was focused, before the
 * keyboard came up: the shell scrolls instead, and the browser brings the focused composer up
 * onto the keyboard, the page moving up whole.
 */
function useKeyboardFreeHeight(pageRef: RefObject<HTMLElement | null>): number | null {
  const [held, setHeld] = useState<number | null>(null);
  useEffect(() => {
    const page = pageRef.current;
    if (!page || !isTouchCapableDevice()) return;
    const onFocusIn = (event: FocusEvent) => {
      if (!isEditable(event.target)) return;
      setHeld((current) => current ?? page.getBoundingClientRect().height);
    };
    const onFocusOut = (event: FocusEvent) => {
      // Moving between two fields on the page keeps the keyboard up.
      if (isEditable(event.relatedTarget) && page.contains(event.relatedTarget as Node)) return;
      setHeld(null);
    };
    page.addEventListener('focusin', onFocusIn);
    page.addEventListener('focusout', onFocusOut);
    return () => {
      page.removeEventListener('focusin', onFocusIn);
      page.removeEventListener('focusout', onFocusOut);
    };
  }, [pageRef]);
  return held;
}
