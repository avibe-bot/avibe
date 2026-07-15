// Window drag/resize gesture helpers (§7.1i). An in-window iframe (a showpage
// window body) steals pointer events once a title-bar/resize gesture drags over it —
// the parent's move handlers stop firing and the drag freezes. The primary fix is
// pointer capture on the gesture element (AppWindow.startGesture); this module is the
// belt-and-braces shield: a transparent overlay over each window body while ANY
// window is mid-gesture, so a stray pointer event can't reach an iframe and the
// cursor can't flicker over it.

/** Whether a window should shield its body while a gesture is active. Only visible
 *  windows have a body to shield — a minimized window is hidden, so skip it. Pure. */
export function shouldShieldWindowBody(gestureActive: boolean, minimized: boolean): boolean {
  return gestureActive && !minimized;
}

/** A transparent, non-iframe overlay filling the window body during a drag/resize.
 *  Any pointer event over the body lands here (a plain div, no handlers) instead of
 *  the iframe document, and the iframe's cursor can't show through. Sits below the
 *  z-30 resize grips and the title bar, so those stay grabbable. Renders nothing when
 *  inactive. */
export const WindowBodyGestureShield: React.FC<{ active: boolean }> = ({ active }) =>
  active ? <div aria-hidden data-gesture-shield className="absolute inset-0 z-20" /> : null;
