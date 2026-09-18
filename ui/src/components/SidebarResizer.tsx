import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

/** The one width the sidebar, every desktop content offset and the Settings
 *  overlay edge read. Declared in `index.css` at MIN_SIDEBAR_WIDTH so the
 *  default shell needs no JS; this component overrides it inline while it is
 *  mounted, which is also how the value reaches the overlay's portal. */
export const SIDEBAR_WIDTH_VAR = '--app-sidebar-w';
/** The shipped width is the minimum, so the default shell is unchanged. */
export const MIN_SIDEBAR_WIDTH = 248;
export const MAX_SIDEBAR_WIDTH = 496;
const KEYBOARD_STEP = 16;

const clampWidth = (width: number) => (
  Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, Math.round(width)))
);

// This component is the only writer, so the inline value is either a width it
// set or nothing at all — no need to resolve the stylesheet's own default.
const readWidth = () => {
  const inline = Number.parseFloat(
    document.documentElement.style.getPropertyValue(SIDEBAR_WIDTH_VAR),
  );
  return Number.isFinite(inline) ? clampWidth(inline) : MIN_SIDEBAR_WIDTH;
};

type Gesture = {
  pointerId: number;
  startX: number;
  startWidth: number;
  /** What the body's own inline cursor/selection were before the drag borrowed
   *  them, so ending the gesture gives those back instead of erasing them. */
  bodyCursor: string;
  bodyUserSelect: string;
};

/**
 * The sidebar's right divider, made draggable. It draws no handle of its own:
 * the strip is an invisible grab area whose right border lands on the sidebar's
 * own 1px divider, so hover, keyboard focus and an active drag recolor that one
 * line and nothing else.
 */
export const SidebarResizer = () => {
  const { t } = useTranslation();
  const [width, setWidth] = useState(readWidth);
  const [dragging, setDragging] = useState(false);
  const gesture = useRef<Gesture | null>(null);

  const apply = useCallback((next: number) => {
    const clamped = clampWidth(next);
    setWidth(clamped);
    document.documentElement.style.setProperty(SIDEBAR_WIDTH_VAR, `${clamped}px`);
  }, []);

  // Pointer capture is released implicitly on up and cancel, so the only thing a
  // gesture leaves behind is the body styling it borrowed for the drag. Only the
  // pointer that started the drag can end it: a second finger's up or cancel
  // arrives here too, and ending on it would drop a gesture still under way.
  const endGesture = useCallback((event?: React.PointerEvent<HTMLDivElement>) => {
    const active = gesture.current;
    if (!active || (event && event.pointerId !== active.pointerId)) return;
    gesture.current = null;
    setDragging(false);
    document.body.style.cursor = active.bodyCursor;
    document.body.style.userSelect = active.bodyUserSelect;
  }, []);

  // Unmount is also how a gesture ends when the viewport leaves desktop layout:
  // give the borrowed styling back, and the width back to the stylesheet, so one
  // mount's drag cannot leak into a later mount or a standalone app tab.
  useEffect(() => () => {
    endGesture();
    document.documentElement.style.removeProperty(SIDEBAR_WIDTH_VAR);
  }, [endGesture]);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    // A drag already under way owns the edge; a second pointer neither takes it
    // over nor gets a gesture of its own.
    if (event.button !== 0 || gesture.current) return;
    event.preventDefault();
    // Capture retargets the rest of the gesture here, so the pointer can travel
    // anywhere — over the content, past the window — and still be handled and
    // released by this element.
    event.currentTarget.setPointerCapture(event.pointerId);
    gesture.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: width,
      bodyCursor: document.body.style.cursor,
      bodyUserSelect: document.body.style.userSelect,
    };
    setDragging(true);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const active = gesture.current;
    if (!active || active.pointerId !== event.pointerId) return;
    // Measured from this gesture's own start, never from the previous frame, so
    // travel beyond a bound is not remembered and repeated drags cannot compound.
    apply(active.startWidth + event.clientX - active.startX);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const next = event.key === 'ArrowLeft' ? width - KEYBOARD_STEP
      : event.key === 'ArrowRight' ? width + KEYBOARD_STEP
        : event.key === 'Home' ? MIN_SIDEBAR_WIDTH
          : event.key === 'End' ? MAX_SIDEBAR_WIDTH
            : null;
    if (next === null) return;
    event.preventDefault();
    apply(next);
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={t('appShell.resizeSidebar')}
      aria-valuenow={width}
      aria-valuemin={MIN_SIDEBAR_WIDTH}
      aria-valuemax={MAX_SIDEBAR_WIDTH}
      tabIndex={0}
      // Read by the Settings overlay, which is dismissed by an interaction
      // outside itself and would otherwise close the moment this edge — which
      // its own left edge follows — was grabbed.
      data-sidebar-resizer="true"
      data-dragging={dragging || undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endGesture}
      onPointerCancel={endGesture}
      onLostPointerCapture={endGesture}
      onKeyDown={onKeyDown}
      className={clsx(
        'absolute inset-y-0 -right-px z-10 w-2 cursor-col-resize border-r border-transparent outline-none transition-colors',
        'hover:border-cyan focus-visible:border-cyan',
        dragging && 'border-cyan',
      )}
    />
  );
};
