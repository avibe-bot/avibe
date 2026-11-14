import { useEffect, useRef, useState, type RefObject } from 'react';

/** How far a pull at the top of a scroller has to travel to refresh. */
export const PULL_THRESHOLD_PX = 64;

/**
 * Pull to refresh on a scroller at its top (the full-screen first-use page only: in the sheet a
 * pull at the top dismisses it). Returns how far the finger has pulled, for the indicator.
 */
export function usePullToRefresh(
  scroller: RefObject<HTMLElement | null>,
  onRefresh: () => void,
  enabled: boolean,
): number {
  const [pulled, setPulled] = useState(0);
  const start = useRef<number | null>(null);
  const distance = useRef(0);
  const refresh = useRef(onRefresh);
  useEffect(() => {
    refresh.current = onRefresh;
  }, [onRefresh]);
  useEffect(() => {
    const element = scroller.current;
    if (!element || !enabled) return undefined;
    const begin = (event: TouchEvent) => {
      start.current = event.touches.length === 1 ? event.touches[0].clientY : null;
      distance.current = 0;
    };
    const move = (event: TouchEvent) => {
      if (start.current === null) return;
      if (element.scrollTop > 0) {
        // Still scrolling the list: only the travel past its top counts as a pull.
        start.current = event.touches[0].clientY;
        distance.current = 0;
        setPulled(0);
        return;
      }
      const delta = event.touches[0].clientY - start.current;
      distance.current = delta > 0 ? Math.min(delta, PULL_THRESHOLD_PX * 2) : 0;
      setPulled(distance.current);
    };
    const end = () => {
      const fire = start.current !== null && distance.current >= PULL_THRESHOLD_PX;
      start.current = null;
      distance.current = 0;
      setPulled(0);
      if (fire) refresh.current();
    };
    element.addEventListener('touchstart', begin, { passive: true });
    element.addEventListener('touchmove', move, { passive: true });
    element.addEventListener('touchend', end);
    element.addEventListener('touchcancel', end);
    return () => {
      element.removeEventListener('touchstart', begin);
      element.removeEventListener('touchmove', move);
      element.removeEventListener('touchend', end);
      element.removeEventListener('touchcancel', end);
    };
  }, [scroller, enabled]);
  return pulled;
}
