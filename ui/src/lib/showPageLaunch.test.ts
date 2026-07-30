import { describe, expect, it } from 'vitest';

import {
  isShowPageWindowDrop,
  SHOW_PAGE_WINDOW_DRAG_THRESHOLD_PX,
  showPageWindowOrigin,
} from './showPageLaunch';

describe('isShowPageWindowDrop', () => {
  const start = { x: 500, y: 80 };

  it('requires a deliberate downward drag', () => {
    expect(
      isShowPageWindowDrop(start, {
        x: start.x,
        y: start.y + SHOW_PAGE_WINDOW_DRAG_THRESHOLD_PX,
      }),
    ).toBe(true);
    expect(
      isShowPageWindowDrop(start, {
        x: start.x + 300,
        y: start.y + SHOW_PAGE_WINDOW_DRAG_THRESHOLD_PX - 1,
      }),
    ).toBe(false);
    expect(isShowPageWindowDrop(start, { x: start.x, y: start.y - 100 })).toBe(false);
  });
});

describe('showPageWindowOrigin', () => {
  it('anchors the title bar near the pointer and keeps it on-screen', () => {
    expect(showPageWindowOrigin({ x: 420, y: 260 })).toEqual({ x: 384, y: 246 });
    expect(showPageWindowOrigin({ x: 4, y: 3 })).toEqual({ x: 8, y: 8 });
  });
});
