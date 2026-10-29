/* @vitest-environment jsdom */

import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { REPLY_LINGER_MS, REPLY_LINGER_RESUME_MS, useReplyLinger } from './useReplyLinger';

describe('useReplyLinger', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('keeps an untouched reply for 20 seconds', () => {
    vi.useFakeTimers();
    const scope = {};
    const { result } = renderHook(() => useReplyLinger({
      scope,
      candidateId: 'reply-1',
      counting: true,
      keyboardHeld: () => false,
    }));

    act(() => { vi.advanceTimersByTime(REPLY_LINGER_MS - 1); });
    expect(result.current.lingeringId).toBe('reply-1');
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current.lingeringId).toBeNull();
  });

  it('pauses the budget and restores at least three seconds after pointer leave', () => {
    vi.useFakeTimers();
    const scope = {};
    let counting = true;
    const { result, rerender } = renderHook(() => useReplyLinger({
      scope,
      candidateId: 'reply-1',
      counting,
      keyboardHeld: () => false,
    }));

    act(() => { vi.advanceTimersByTime(REPLY_LINGER_MS - 1_000); });
    counting = false;
    rerender();
    act(() => { vi.advanceTimersByTime(REPLY_LINGER_MS); });
    expect(result.current.lingeringId).toBe('reply-1');
    act(() => { result.current.keepAtLeast(REPLY_LINGER_RESUME_MS); });
    counting = true;
    rerender();
    act(() => { vi.advanceTimersByTime(REPLY_LINGER_RESUME_MS - 1); });
    expect(result.current.lingeringId).toBe('reply-1');
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current.lingeringId).toBeNull();
  });
});
