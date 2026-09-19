import { useEffect } from 'react';
import { isSoftKeyboardOpen, isTouchCapableDevice } from './softKeyboard';

// iOS Safari keeps the layout viewport (and 100dvh) at full height when the
// virtual keyboard opens — only the VISUAL viewport shrinks — so a bottom-pinned
// chat composer ends up stranded with a large gap above the keyboard (dvh alone
// doesn't fix it on iOS, and interactive-widget=resizes-content isn't supported
// there). Mirror window.visualViewport into four CSS vars (rAF-throttled): the
// part of the window a person can actually see, as a rectangle — --app-vvw /
// --app-vvh for its size and --app-vvl / --app-vvt for where it starts, since
// iOS also PANS the visual viewport to keep a focused field in sight and
// `position: fixed` is laid out against the layout viewport. A size alone says
// how much can be seen but not which part, so all four are written together.
//
// NB: the MOBILE shell deliberately does NOT consume this — sizing the shell to
// it mid-focus fought iOS's own scroll-into-view and flung the input off the top
// (the mobile shell is a static locked column instead, see AppShell/index.css).
// Consumers are the md+ chat (iPad / phone-landscape), which uses the desktop
// layout and so cannot use the mobile body-lock — sizing that chat to the visual
// viewport keeps its composer above the soft keyboard — and the setup
// connection dialog, which is reached before that shell exists and so keeps the
// same vars current itself. Both may be mounted at once: they write identical
// values, and the cleanup below deliberately removes only this effect's
// listeners, never the properties, so one unmounting cannot strand the other.
//
// Gated so it tracks ONLY the soft keyboard: (1) touch devices — non-touch desktops
// have no keyboard, so the var must stay at its 100dvh default; (2) skip a
// keyboard-less pinch-zoom — trackpad/gesture zoom ALSO shrinks visualViewport.height
// (with scale > 1), and mirroring that would drag the bottom-pinned composer up off
// the bottom (worse the more you zoom). Zoom can coexist with the keyboard though
// (iPad: zoom first, then focus the composer), and there the inset is still needed,
// so we keep applying it whenever isSoftKeyboardOpen() and only bail on a zoom with
// no keyboard. Refs:
//   https://www.bram.us/2021/09/13/prevent-items-from-being-hidden-underneath-the-virtual-keyboard-by-means-of-the-virtualkeyboard-api/
//   https://dev.to/franciscomoretti/fix-mobile-keyboard-overlap-with-visualviewport-3a4a
const VARS = ['--app-vvh', '--app-vvt', '--app-vvw', '--app-vvl'] as const;

export function useViewportHeightVar(): void {
  useEffect(() => {
    // Non-touch desktops have no soft keyboard, so this var must never move there;
    // bailing out also makes the chat immune to trackpad pinch-zoom (which would
    // otherwise shrink --app-vvh and lift the composer). CSS keeps its 100dvh
    // default — a layout-viewport unit pinch-zoom can't touch.
    if (!isTouchCapableDevice()) return;
    const vv = window.visualViewport;
    // No visualViewport (older browsers / SSR) → CSS keeps its 100dvh default.
    if (!vv) return;
    let raf = 0;
    const apply = () => {
      raf = 0;
      // Touch devices can pinch-zoom too (iPad, touch laptops), which also shrinks
      // vv.height with scale > 1. A keyboard-less zoom must NOT drive the inset (it
      // would lift the composer off the bottom), so drop the overrides and fall back
      // to the layout-viewport defaults. But zoom and the keyboard can be up together
      // (zoom, then focus the
      // composer) — then we still need the inset, so only bail when the keyboard is
      // closed; isSoftKeyboardOpen() is scale-aware, so it still fires while zoomed.
      const root = document.documentElement.style;
      if (vv.scale > 1 && !isSoftKeyboardOpen()) {
        for (const name of VARS) root.removeProperty(name);
        return;
      }
      // Where the visible band starts, beside how big it is. iOS pans the visual
      // viewport over the layout viewport to keep a focused field in sight, and
      // `position: fixed` is laid out against the LAYOUT viewport — so a size
      // alone describes how much a person can see but not which part, and
      // anything centred on it drifts by the pan. All four are written and
      // cleared together, so no two of them can be read from different moments.
      // (`scroll` is already listened to: a pan moves the offsets without
      // changing the size.)
      root.setProperty('--app-vvh', `${Math.round(vv.height)}px`);
      root.setProperty('--app-vvt', `${Math.round(vv.offsetTop)}px`);
      root.setProperty('--app-vvw', `${Math.round(vv.width)}px`);
      root.setProperty('--app-vvl', `${Math.round(vv.offsetLeft)}px`);
    };
    const schedule = () => {
      if (raf) return;
      raf = requestAnimationFrame(apply);
    };
    apply();
    vv.addEventListener('resize', schedule);
    vv.addEventListener('scroll', schedule);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      vv.removeEventListener('resize', schedule);
      vv.removeEventListener('scroll', schedule);
    };
  }, []);
}
