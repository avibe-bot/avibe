import { type RefObject, useEffect } from 'react';
import { hasNativeCapability } from './native';
import { isTextField } from './softKeyboard';

// Keep a bottom-pinned composer glued to the on-screen keyboard on mobile iOS
// Safari (incl. the iOS 26 regression where `position: fixed` stops being
// honored, `dvh` under-resolves, and `visualViewport` values are
// self-contradictory — so the composer either flies off the top on focus or
// floats above the keyboard).
//
// The surface is sized to the TRUE visible bottom while a text field has focus
// (visualViewport.offsetTop + visualViewport.height; 100dvh under-resolves with
// the keyboard open), and released to its CSS size once none has. When:
//   • at once on each visualViewport resize: iOS reports the keyboard's final
//     size when it starts to open or close, so the composer takes its place as
//     the keyboard starts to move. A focus our code makes (a tap the
//     hold-to-talk box took) gets no pan from iOS, and the keyboard covered the
//     composer until the settled pass below; a close left the composer halfway
//     up the screen until it did;
//   • again once the viewport SETTLES (debounced), for scrolls: reacting to
//     every pan frame of iOS's own scroll-into-view over-corrects.
// A text field is an input, a textarea, or rich text: the chat composer is a
// contenteditable editor. Counting only the first two released the surface
// whenever focus came back to the composer (the editor refocusing itself after a
// send, a tap from the transcript), and the composer sank under an open
// keyboard. The scrollIntoView backstop stays with inputs and textareas.
// iOS 26: offsetTop can stay > 0 after the keyboard closes; releasing the
// surface nudges the scroll so it is read again.
//
// Only the surface's height is written here, never the transcript's scroll: a
// transcript resized by it is held by its own anchor and follow-tail.
//
// `containerRef` is a `position: fixed` surface that exists only on the mobile
// layout, so the correction is gated to the mobile breakpoint — and re-evaluated
// live (the chat page stays mounted across orientation changes, so a rotate into
// the desktop/`md` layout must release the listeners AND clear any stale inline
// height that would otherwise override `md:h-[var(--app-vvh)]`).
export function useIosKeyboardInset(containerRef: RefObject<HTMLDivElement | null>): void {
  useEffect(() => {
    if (hasNativeCapability('keyboardResizesViewport')) return;
    const vv = window.visualViewport;
    if (!vv) return;
    const mql = window.matchMedia('(max-width: 767px)');

    let settle = 0;
    let active = false;

    // ``afterBlur``: the field just lost focus (the original nudge point). Else
    // nudge only when a held height is let go, so a plain pan or zoom of the
    // viewport with no keyboard is never fought.
    const release = ({ afterBlur }: { afterBlur: boolean }) => {
      const el = containerRef.current;
      const held = !!el?.style.height;
      if (el && held) el.style.height = '';
      // iOS 26: offsetTop can stay > 0 after dismiss; a 1px scroll forces recalc.
      if ((held || afterBlur) && vv.offsetTop > 0) {
        window.scrollBy(0, -1);
        window.scrollBy(0, 1);
      }
    };
    const correct = ({ backstop }: { backstop: boolean }) => {
      const focused = document.activeElement;
      // No field has focus: no keyboard to keep clear of.
      if (!isTextField(focused)) {
        release({ afterBlur: false });
        return;
      }
      const el = containerRef.current;
      // True bottom of the visible area, in layout coords (see header comment).
      if (el) el.style.height = `${Math.round(vv.offsetTop + vv.height)}px`;
      if (backstop && (focused!.tagName === 'TEXTAREA' || focused!.tagName === 'INPUT')) {
        (focused as HTMLElement).scrollIntoView({ block: 'end', behavior: 'smooth' });
      }
    };
    const settled = () => {
      window.clearTimeout(settle);
      settle = window.setTimeout(() => correct({ backstop: true }), 140);
    };
    const onResize = () => {
      correct({ backstop: false });
      settled();
    };
    const onFocusOut = () => {
      window.setTimeout(() => {
        // Focus moved to ANOTHER field (e.g. title → composer) with the keyboard
        // still up — keep the inset; releasing here would expand the surface back
        // behind the keyboard and obscure the composer until the next vv event.
        if (isTextField(document.activeElement)) return;
        release({ afterBlur: true });
      }, 120);
    };

    const activate = () => {
      if (active) return;
      active = true;
      vv.addEventListener('resize', onResize);
      vv.addEventListener('scroll', settled);
      document.addEventListener('focusout', onFocusOut);
    };
    const deactivate = () => {
      if (!active) return;
      active = false;
      window.clearTimeout(settle);
      vv.removeEventListener('resize', onResize);
      vv.removeEventListener('scroll', settled);
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
