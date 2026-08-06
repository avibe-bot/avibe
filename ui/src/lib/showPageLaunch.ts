export interface ViewportPoint {
  x: number;
  y: number;
}

export const SHOW_PAGE_WINDOW_DRAG_THRESHOLD_PX = 48;
export const SHOW_PAGE_DOCK_DROP_SELECTOR = '[data-show-page-dock-drop-target]';

/** A free drop opens a window only after a deliberate downward drag. */
export function isShowPageWindowDrop(start: ViewportPoint, current: ViewportPoint): boolean {
  return current.y - start.y >= SHOW_PAGE_WINDOW_DRAG_THRESHOLD_PX;
}

/** Use the release point as the new window's origin, bounded by the viewport edge. */
export function showPageWindowOrigin(point: ViewportPoint): ViewportPoint {
  return {
    x: Math.max(8, Math.round(point.x)),
    y: Math.max(8, Math.round(point.y)),
  };
}
