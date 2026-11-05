import { type RefObject, useEffect } from 'react';
import { isNativeShell } from './native';

// Keep a bottom-pinned composer glued to the on-screen keyboard on mobile iOS
// Safari (incl. the iOS 26 regression where `position: fixed` stops being
// honored, `dvh` under-resolves, and `visualViewport` values are
// self-contradictory — so the composer either flies off the top on focus or
// floats above the keyboard).
//
// Approach (validated on-device): do NOT fight the keyboard's open animation —
// reacting to every visualViewport frame over-corrects and flings the input even
// farther. Instead, let iOS do its natural pan, then ONCE the viewport SETTLES
// (debounced) apply a single gentle correction:
//   • size the fixed full-screen surface to the TRUE visible bottom
//     (visualViewport.offsetTop + visualViewport.height) — 100dvh under-resolves
//     with the keyboard open, which is why the composer stops short of the bottom;
//   • pull the focused field into view (scrollIntoView) as a backstop.
// On blur, restore full height and nudge the scroll to work around the iOS 26 bug
// where visualViewport.offsetTop fails to revert after the keyboard closes.
//
// `containerRef` is a `position: fixed` surface that exists only on the mobile
// layout, so the correction is gated to the mobile breakpoint — and re-evaluated
// live (the chat page stays mounted across orientation changes, so a rotate into
// the desktop/`md` layout must release the listeners AND clear any stale inline
// height that would otherwise override `md:h-[var(--app-vvh)]`).
export function useIosKeyboardInset(containerRef: RefObject<HTMLDivElement | null>): void {
  useEffect(() => {
    if (isNativeShell()) return;
    const vv = window.visualViewport;
    if (!vv) return;
    const mql = window.matchMedia('(max-width: 767px)');

    let settle = 0;
    let active = false;

    const isEditable = (el: Element | null): boolean =>
      !!el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT');

    const correct = () => {
      const el = containerRef.current;
      // True bottom of the visible area, in layout coords (see header comment).
      if (el) el.style.height = `${Math.round(vv.offsetTop + vv.height)}px`;
      if (isEditable(document.activeElement)) {
        (document.activeElement as HTMLElement).scrollIntoView({ block: 'end', behavior: 'smooth' });
      }
    };
    // Debounce: nothing moves DURING the open/close animation; correct once the
    // visual viewport stops changing.
    const onViewport = () => {
      window.clearTimeout(settle);
      settle = window.setTimeout(correct, 140);
    };
    const onFocusOut = () => {
      window.setTimeout(() => {
        // Focus moved to ANOTHER field (e.g. title → composer) with the keyboard
        // still up — keep the inset; resetting here would expand the surface back
        // behind the keyboard and obscure the composer until the next vv event.
        if (isEditable(document.activeElement)) return;
        const el = containerRef.current;
        if (el) el.style.height = '';
        // iOS 26: offsetTop can stay > 0 after dismiss; a 1px scroll forces recalc.
        if (window.visualViewport && window.visualViewport.offsetTop > 0) {
          window.scrollBy(0, -1);
          window.scrollBy(0, 1);
        }
      }, 120);
    };

    const activate = () => {
      if (active) return;
      active = true;
      vv.addEventListener('resize', onViewport);
      vv.addEventListener('scroll', onViewport);
      document.addEventListener('focusout', onFocusOut);
    };
    const deactivate = () => {
      if (!active) return;
      active = false;
      window.clearTimeout(settle);
      vv.removeEventListener('resize', onViewport);
      vv.removeEventListener('scroll', onViewport);
      document.removeEventListener('focusout', onFocusOut);
      // Clear any stale inline height so the desktop/md layout takes over.
      const el = containerRef.current;
      if (el) el.style.height = '';
    };

    // Sync to the breakpoint now and whenever it changes (orientation/resize).
    const sync = () => (mql.matches ? activate() : deactivate());
    sync();
    mql.addEventListener('change', sync);

    return () => {
      mql.removeEventListener('change', sync);
      deactivate();
    };
  }, [containerRef]);
}
