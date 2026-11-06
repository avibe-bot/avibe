// @vitest-environment jsdom
// Helpers of the motion tokens (docs/plans/web-ui-motion.md §4).
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { timeToProgress, useDisclosure } from './motion';

describe('timeToProgress', () => {
  it('finds when an accelerating exit has faded a copy to 30% and to 10%', () => {
    // `--motion-ease-exit` on `instant`: slow at first, so the copy is still above 30% at 70 ms.
    const exit = { duration: 100, easing: 'cubic-bezier(0.4, 0, 1, 1)' };
    expect(timeToProgress(exit, 0.7)).toBeGreaterThan(78);
    expect(timeToProgress(exit, 0.7)).toBeLessThan(82);
    expect(timeToProgress(exit, 0.9)).toBeGreaterThan(90);
    expect(timeToProgress(exit, 0.9)).toBeLessThan(95);
  });

  it('is early on a decelerating curve, and proportional on any other easing', () => {
    expect(timeToProgress({ duration: 200, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' }, 0.7)).toBeLessThan(50);
    expect(timeToProgress({ duration: 200, easing: 'linear' }, 0.7)).toBe(140);
    expect(timeToProgress({ duration: 200, easing: 'ease-out' }, 0.5)).toBe(100);
  });
});

describe('useDisclosure', () => {
  afterEach(() => {
    document.documentElement.style.removeProperty('--motion-duration-instant');
    vi.useRealTimers();
  });

  it('keeps what closes for one `instant`, closing, then lets it go', () => {
    vi.useFakeTimers();
    document.documentElement.style.setProperty('--motion-duration-instant', '100ms');
    const { result, rerender } = renderHook(({ open }) => useDisclosure(open), { initialProps: { open: true } });
    expect(result.current).toEqual({ present: true, closing: false });
    rerender({ open: false });
    expect(result.current).toEqual({ present: true, closing: true });
    act(() => { vi.advanceTimersByTime(99); });
    expect(result.current.present).toBe(true);
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current).toEqual({ present: false, closing: false });
    // Opened again while closing, it is simply open.
    rerender({ open: true });
    rerender({ open: false });
    rerender({ open: true });
    expect(result.current).toEqual({ present: true, closing: false });
  });

  it('lets go at once where there is no token to time it by', () => {
    const { result, rerender } = renderHook(({ open }) => useDisclosure(open), { initialProps: { open: true } });
    rerender({ open: false });
    expect(result.current).toEqual({ present: false, closing: false });
  });
});
