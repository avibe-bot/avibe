import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';

import { isTouchCapableDevice } from '../../lib/softKeyboard';

// The home's layout (ui/design: home/home/*, approved with the layout spec in #3174):
// 1. One order: greeting → Getting started (while something is missing) → tip of the day (with a
//    model, unless hidden today) → the four asks → the composer, the group's last block, never
//    pinned to the page's bottom.
// 2. One centered group, a little high: the free space above and below it splits 2 : 3. A block
//    that comes or goes re-centers the group; the order stays.
// 3. One gap between any two blocks: 28 on a desktop, 16 on a phone. When the page is short the
//    free space shrinks first (to 24 on a desktop, 8 on a phone), then the page scrolls; the gap
//    never shrinks.
// 4. One width for every block: 768 at most on a desktop (a large window keeps it), the screen less
//    16 a side on a phone, edges aligned.
// 5. One lead at a time: with no model only "Give <Bot> a model" is accented, the greeting
//    introduces the Bot, the asks are quiet, and the composer is disabled with "Add a model" where
//    its route goes (the same page as "Add one", Settings · Models). With a model the greeting asks
//    what is next, and the asks and the tip invite a first try.
// 6. Buttons: only the one lead is solid; a card's small actions are text buttons, no border or
//    fill, a soft wash on hover. The composer's route shows the model and effort alone.
// 7. A phone: no face in the greeting (the tab bar's center is the main Bot); Getting started shows
//    one item. A member: the project chips sit right above the composer, 8 apart, one block with it.
// 8. The phone keyboard: the group does not re-center in the shorter visible area. The page keeps
//    its keyboard-free height and moves up whole, so the composer rests on the keyboard and the
//    header and blocks above may move off screen. Safari/PWA lets the keyboard cover the tab bar;
//    the native shell removes the tab bar and its clearance, then reveals the complete composer in
//    UIKit's resized WebView.

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
      <div aria-hidden="true" data-home-space="above" className="min-h-2 shrink grow-2 md:min-h-6" />
      <div className="mx-auto flex w-full max-w-3xl shrink-0 flex-col gap-4 md:gap-7">
        {children}
      </div>
      <div aria-hidden="true" data-home-space="below" className="min-h-2 shrink grow-3 md:min-h-6" />
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
 * keyboard came up: the shell scrolls instead, and the focused composer can move onto the
 * keyboard, the page moving up whole. In the native shell, AppShell separately removes its phone
 * tab bar and the Composer reveals its full action row after UIKit's viewport resize settles.
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
