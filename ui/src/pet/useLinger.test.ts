// @vitest-environment jsdom

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BUBBLE_LINGER_MS, useLinger } from './useLinger';

describe('useLinger', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  const render = (initial: { restartKey: number; paused: boolean }) =>
    renderHook((props: { restartKey: number; paused: boolean }) => useLinger(props.restartKey, props.paused), { initialProps: initial });

  it('stays closed until the first restart', () => {
    const { result } = render({ restartKey: 0, paused: false });
    expect(result.current).toBe(false);
    act(() => { vi.advanceTimersByTime(BUBBLE_LINGER_MS * 2); });
    expect(result.current).toBe(false);
  });

  it('closes after the duration of unpaused time', () => {
    const { result, rerender } = render({ restartKey: 0, paused: false });
    rerender({ restartKey: 1, paused: false });
    expect(result.current).toBe(true);
    act(() => { vi.advanceTimersByTime(BUBBLE_LINGER_MS - 1); });
    expect(result.current).toBe(true);
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current).toBe(false);
  });

  it('does not count paused time, and resumes with what was left', () => {
    const { result, rerender } = render({ restartKey: 1, paused: false });
    act(() => { vi.advanceTimersByTime(20_000); });
    rerender({ restartKey: 1, paused: true });
    act(() => { vi.advanceTimersByTime(BUBBLE_LINGER_MS * 3); });
    expect(result.current).toBe(true);
    rerender({ restartKey: 1, paused: false });
    act(() => { vi.advanceTimersByTime(9_999); });
    expect(result.current).toBe(true);
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current).toBe(false);
  });

  it('starts over on a restart, including after it closed', () => {
    const { result, rerender } = render({ restartKey: 1, paused: false });
    act(() => { vi.advanceTimersByTime(25_000); });
    rerender({ restartKey: 2, paused: false });
    act(() => { vi.advanceTimersByTime(25_000); });
    expect(result.current).toBe(true);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(result.current).toBe(false);
    rerender({ restartKey: 3, paused: false });
    expect(result.current).toBe(true);
  });
});
