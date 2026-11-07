import { useEffect, useState } from 'react';

import { hasNativeCapability } from './native';
import { isTextField, SOFT_KEYBOARD_MIN_DELTA } from './softKeyboard';

const MOBILE_QUERY = '(max-width: 767px)';
const KEYBOARD_CLOSE_DELTA = 60;
const VIEWPORT_GEOMETRY_EPSILON = 2;
const VIEWPORT_SETTLE_MS = 140;
const REVEAL_WINDOW_MS = 900;

const viewportHeight = (viewport: VisualViewport | null): number =>
  viewport?.height ?? window.innerHeight;

const viewportScale = (viewport: VisualViewport | null): number =>
  viewport?.scale ?? 1;

const viewportLayoutWidth = (viewport: VisualViewport | null): number =>
  viewport ? viewport.width * viewport.scale : window.innerWidth;

/**
 * The native shell shrinks the whole WKWebView to `keyboardLayoutGuide`, so a
 * fixed phone tab bar would otherwise occupy the reduced viewport above the
 * keyboard. Hide that chrome only after a focused editor actually caused a
 * substantial viewport contraction. Focus alone is not enough: an attached
 * hardware keyboard leaves the tabs in place.
 */
export function useNativeKeyboardOpen(): boolean {
  const resizesViewport = hasNativeCapability('keyboardResizesViewport');
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!resizesViewport) {
      return undefined;
    }

    const viewport = window.visualViewport;
    const mobile = window.matchMedia(MOBILE_QUERY);
    let baseline = viewportHeight(viewport);
    let baselineWidth = viewportLayoutWidth(viewport);
    let keyboardOpen = false;
    let syncFrame = 0;

    const sync = () => {
      syncFrame = 0;
      const height = viewportHeight(viewport);
      const width = viewportLayoutWidth(viewport);
      const editableFocused = isTextField(document.activeElement);

      if (!mobile.matches) {
        baseline = height;
        baselineWidth = width;
        keyboardOpen = false;
        setOpen(false);
        return;
      }

      if (Math.abs(width - baselineWidth) > VIEWPORT_GEOMETRY_EPSILON) {
        baselineWidth = width;
        // A known software keyboard can survive rotation. Keep that state until
        // the resized viewport recovers; a hardware-keyboard focus has no such
        // state and rebases without hiding the phone chrome.
        keyboardOpen = keyboardOpen && editableFocused;
        baseline = keyboardOpen
          ? height + SOFT_KEYBOARD_MIN_DELTA / viewportScale(viewport)
          : height;
        setOpen(keyboardOpen);
        return;
      }

      if (!editableFocused && !keyboardOpen) {
        baseline = height;
        baselineWidth = width;
        setOpen(false);
        return;
      }

      if (height > baseline) baseline = height;
      const delta = (baseline - height) * viewportScale(viewport);
      const next = delta > (keyboardOpen ? KEYBOARD_CLOSE_DELTA : SOFT_KEYBOARD_MIN_DELTA);
      keyboardOpen = next;
      if (!next && !editableFocused) baseline = height;
      setOpen(next);
    };

    const scheduleSync = () => {
      window.cancelAnimationFrame(syncFrame);
      syncFrame = window.requestAnimationFrame(sync);
    };

    viewport?.addEventListener('resize', scheduleSync);
    window.addEventListener('resize', scheduleSync);
    document.addEventListener('focusin', sync);
    document.addEventListener('focusout', sync);
    mobile.addEventListener('change', scheduleSync);
    sync();

    return () => {
      window.cancelAnimationFrame(syncFrame);
      viewport?.removeEventListener('resize', scheduleSync);
      window.removeEventListener('resize', scheduleSync);
      document.removeEventListener('focusin', sync);
      document.removeEventListener('focusout', sync);
      mobile.removeEventListener('change', scheduleSync);
    };
  }, [resizesViewport]);

  return resizesViewport && open;
}

/**
 * A Home suggestion focuses the shared composer before UIKit has finished
 * shrinking the WKWebView. Keep listening through that animation and reveal
 * the whole composer after each settled viewport change.
 */
export function revealComposerAfterNativeKeyboardResize(target: HTMLElement | null): () => void {
  if (
    !target
    || !hasNativeCapability('keyboardResizesViewport')
    || !window.matchMedia(MOBILE_QUERY).matches
  ) {
    return () => undefined;
  }

  const viewport = window.visualViewport;
  let settleTimer = 0;
  let stopTimer = 0;
  let firstFrame = 0;
  let secondFrame = 0;
  let active = true;

  const reveal = () => {
    if (!active || !target.isConnected) return;
    const focused = document.activeElement;
    if (!(focused instanceof Element) || !target.contains(focused)) return;

    const rect = target.getBoundingClientRect();
    const visibleTop = viewport?.offsetTop ?? 0;
    const visibleBottom = visibleTop + viewportHeight(viewport);
    if (rect.top < visibleTop || rect.bottom > visibleBottom) {
      target.scrollIntoView({ block: 'end', inline: 'nearest' });
    }
  };

  const settle = () => {
    window.clearTimeout(settleTimer);
    settleTimer = window.setTimeout(reveal, VIEWPORT_SETTLE_MS);
  };

  const cleanup = () => {
    if (!active) return;
    active = false;
    window.clearTimeout(settleTimer);
    window.clearTimeout(stopTimer);
    window.cancelAnimationFrame(firstFrame);
    window.cancelAnimationFrame(secondFrame);
    viewport?.removeEventListener('resize', settle);
    window.removeEventListener('resize', settle);
  };

  viewport?.addEventListener('resize', settle);
  window.addEventListener('resize', settle);
  firstFrame = window.requestAnimationFrame(() => {
    secondFrame = window.requestAnimationFrame(reveal);
  });
  settle();
  stopTimer = window.setTimeout(() => {
    reveal();
    cleanup();
  }, REVEAL_WINDOW_MS);

  return cleanup;
}
