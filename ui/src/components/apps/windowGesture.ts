// Window drag/resize gesture helpers (§7.1i). An in-window iframe (a showpage
// window body) steals pointer events once a title-bar/resize gesture drags over it —
// the parent's move handlers stop firing and the drag freezes. The primary fix is
// pointer capture on the gesture element (AppWindow.startGesture); the belt-and-braces
// shield is a transparent overlay over each window body while ANY window is mid-gesture,
// so a stray pointer event can't reach an iframe and the cursor can't flicker over it.
// This module owns when to arm it; `WindowBodyGestureShield.tsx` renders it.

/** Whether a window should shield its body while a gesture is active. Only visible
 *  windows have a body to shield — a minimized window is hidden, so skip it. Pure. */
export function shouldShieldWindowBody(gestureActive: boolean, minimized: boolean): boolean {
  return gestureActive && !minimized;
}
