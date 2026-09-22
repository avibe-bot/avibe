import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import {
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  SIDEBAR_WIDTH_VAR,
  clampSidebarWidth as clampWidth,
  currentViewportWidth,
  maxSidebarWidth,
} from '../lib/sidebarWidth';

const KEYBOARD_STEP = 16;

// This component is the only writer, so the inline value is either a width it
// set or nothing at all — no need to resolve the stylesheet's own default.
// Unclamped on purpose: it is what the DOM currently shows, which is the only
// thing a viewport change can find too wide.
const readPublishedWidth = () => {
  const inline = Number.parseFloat(
    document.documentElement.style.getPropertyValue(SIDEBAR_WIDTH_VAR),
  );
  return Number.isFinite(inline) ? inline : null;
};

const readWidth = () => clampWidth(readPublishedWidth() ?? MIN_SIDEBAR_WIDTH);

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
  const [maxWidth, setMaxWidth] = useState(() => maxSidebarWidth(currentViewportWidth()));
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

  // Narrowing the window after a wide drag reaches the same starved layout the
  // cap exists to prevent — the drag is simply earlier in time — so the cap has
  // to follow the viewport, not just the gesture. Re-clamp only when the width
  // no longer fits, so an untouched shell keeps needing no JS at all.
  useEffect(() => {
    const sync = () => {
      const max = maxSidebarWidth(window.innerWidth);
      setMaxWidth(max);
      const published = readPublishedWidth();
      if (published !== null && published > max) apply(max);
    };
    window.addEventListener('resize', sync);
    return () => window.removeEventListener('resize', sync);
  }, [apply]);

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
      aria-valuemax={maxWidth}
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
      // `touch-none` is load-bearing wherever a touchscreen reports the desktop
      // breakpoint — a tablet in landscape, a convertible laptop. Pointer capture
      // does not win the browser's touch-action negotiation, so with the default
      // the native pan claims the finger and cancels the drag part-way.
      className={clsx(
        'absolute inset-y-0 -right-px z-10 w-2 cursor-col-resize touch-none border-r border-transparent outline-none transition-colors',
        'hover:border-cyan focus-visible:border-cyan',
        dragging && 'border-cyan',
      )}
    />
  );
};
