import { describe, expect, it, vi } from 'vitest';

import { startVisibleTicker, VISIBLE_NOW_INTERVAL_MS } from './useVisibleNow';
import type { VisibleTickerHost } from './useVisibleNow';

describe('startVisibleTicker', () => {
  it('ticks every 30s only while visible and re-syncs on return', () => {
    let visible = true;
    let visibilityListener: (() => void) | null = null;
    let intervalCallback: (() => void) | null = null;
    const tick = vi.fn();
    const host: VisibleTickerHost = {
      isVisible: () => visible,
      setInterval: vi.fn((callback, intervalMs) => {
        expect(intervalMs).toBe(VISIBLE_NOW_INTERVAL_MS);
        intervalCallback = callback;
        return 7;
      }),
      clearInterval: vi.fn(),
      addVisibilityListener: vi.fn((callback) => {
        visibilityListener = callback;
      }),
      removeVisibilityListener: vi.fn(),
    };

    const cleanup = startVisibleTicker(tick, VISIBLE_NOW_INTERVAL_MS, host);
    expect(tick).toHaveBeenCalledTimes(1);
    intervalCallback?.();
    expect(tick).toHaveBeenCalledTimes(2);

    visible = false;
    visibilityListener?.();
    expect(host.clearInterval).toHaveBeenCalledWith(7);

    visible = true;
    visibilityListener?.();
    expect(tick).toHaveBeenCalledTimes(3);
    expect(host.setInterval).toHaveBeenCalledTimes(2);

    cleanup();
    expect(host.removeVisibilityListener).toHaveBeenCalledWith(visibilityListener);
  });

  it('does not start a timer in a hidden document', () => {
    let visibilityListener: (() => void) | null = null;
    const tick = vi.fn();
    const host: VisibleTickerHost = {
      isVisible: () => false,
      setInterval: vi.fn(() => 9),
      clearInterval: vi.fn(),
      addVisibilityListener: vi.fn((callback) => {
        visibilityListener = callback;
      }),
      removeVisibilityListener: vi.fn(),
    };

    const cleanup = startVisibleTicker(tick, VISIBLE_NOW_INTERVAL_MS, host);
    expect(tick).not.toHaveBeenCalled();
    expect(host.setInterval).not.toHaveBeenCalled();
    cleanup();
    expect(host.removeVisibilityListener).toHaveBeenCalledWith(visibilityListener);
  });
});
