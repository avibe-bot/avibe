/** A transparent, non-iframe overlay filling the window body during a drag/resize.
 *  Any pointer event over the body lands here (a plain div, no handlers) instead of
 *  the iframe document, and the iframe's cursor can't show through. Sits below the
 *  z-30 resize grips and the title bar, so those stay grabbable. Renders nothing when
 *  inactive. See `windowGesture.ts` for why the shield exists at all. */
export const WindowBodyGestureShield: React.FC<{ active: boolean }> = ({ active }) =>
  active ? <div aria-hidden data-gesture-shield className="absolute inset-0 z-20" /> : null;
