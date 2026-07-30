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

/** Put the new window's title bar just above and left of the release point. */
export function showPageWindowOrigin(point: ViewportPoint): ViewportPoint {
  return {
    x: Math.max(8, Math.round(point.x - 36)),
    y: Math.max(8, Math.round(point.y - 14)),
  };
}
