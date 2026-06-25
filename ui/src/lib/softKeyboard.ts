// Cross-platform "is the on-screen keyboard open?" check.
//
// It must work whether the keyboard OVERLAYS the viewport (iOS Safari: window
// .innerHeight stays full, only visualViewport.height shrinks) OR RESIZES it
// (Android Chrome with interactive-widget=resizes-content: innerHeight AND
// visualViewport.height shrink together, so an innerHeight−visualViewport delta
// reads ~0 — https://developer.chrome.com/blog/viewport-resize-behavior/). The
// signal that survives both is the visual-viewport height vs the LARGEST height
// seen while no keyboard was up (the "resting" height): the keyboard only ever
// shrinks the visible viewport, and by far more than address-bar show/hide.

let restingHeight = 0;

// True when a text field is focused — i.e. the soft keyboard could be up — so the
// resize handler knows whether a shrink is "keyboard" or just a window/orientation
// change.
function isEditableFocused(): boolean {
  const el = typeof document !== 'undefined' ? (document.activeElement as HTMLElement | null) : null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable;
}

if (typeof window !== 'undefined' && window.visualViewport) {
  const vv = window.visualViewport;
  // Initialised at module load (app start, keyboard closed) → a keyboard-free baseline.
  restingHeight = vv.height;
  vv.addEventListener('resize', () => {
    // While NO text field is focused the keyboard is closed, so the current height
    // IS the resting baseline — this recomputes it on window resize / orientation
    // change / address-bar show-hide (so a shrunk desktop window doesn't read as a
    // keyboard). While a field is focused, only let the baseline grow (address bar
    // hiding); never let the keyboard itself lower it.
    if (!isEditableFocused() || vv.height > restingHeight) restingHeight = vv.height;
  });
}

// ~250–300px keyboard vs ~60–100px address-bar variation → a 150px floor cleanly
// separates "keyboard up" from address-bar collapse/expand.
const KEYBOARD_MIN_DELTA = 150;

// Only touch-capable devices have an on-screen keyboard. Gating on this keeps
// isSoftKeyboardOpen() false on desktop regardless of window resizes (so Enter
// always sends there — no false positives from a shorter window), while a
// hardware keyboard on a touch device still reads false because it doesn't shrink
// the viewport. So the resting-height heuristic only ever runs where a soft
// keyboard can actually exist.
const isTouchDevice = typeof navigator !== 'undefined' && navigator.maxTouchPoints > 0;

export function isSoftKeyboardOpen(): boolean {
  if (!isTouchDevice || typeof window === 'undefined' || !window.visualViewport) return false;
  const vv = window.visualViewport;
  // vv.height (and so the resting-height delta) is in CSS px, which pinch-zoom
  // shrinks by ~1/scale — but the OS keyboard occupies a fixed slice of the physical
  // screen, so at scale > 1 the measured delta reads ~keyboardCssPx/scale and can dip
  // under the fixed threshold while the keyboard is genuinely open (e.g. iPad: zoom,
  // then focus a field). Multiply the delta back up by scale to compare in unzoomed
  // CSS px; at scale 1 this is a no-op, so non-zoomed behavior is unchanged.
  return (restingHeight - vv.height) * vv.scale > KEYBOARD_MIN_DELTA;
}

// True on devices with an on-screen keyboard (touch). Callers gate on this to
// avoid auto-focusing inputs where focusing would pop the soft keyboard (e.g.
// opening a chat on mobile); "desktop only" is ``!isTouchCapableDevice()``.
export function isTouchCapableDevice(): boolean {
  return isTouchDevice;
}
