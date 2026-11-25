import { afterEach, expect, it, vi } from 'vitest';

import { afterDeadline } from './useDesktopClaim';

afterEach(() => { vi.useRealTimers(); });

// A Retry-After of seven digits is longer than one timer can hold (about 24.8 days).
it('waits out a deadline longer than one timer, and never unlocks on an early wake', () => {
  vi.useFakeTimers();
  const start = Date.now();
  const due = vi.fn();
  afterDeadline(start + 40 * 86_400_000, due);
  vi.advanceTimersByTime(2 ** 31 - 1);
  expect(due).not.toHaveBeenCalled();
  vi.advanceTimersByTime(40 * 86_400_000 - (2 ** 31 - 1) - 1);
  expect(due).not.toHaveBeenCalled();
  vi.advanceTimersByTime(1);
  expect(due).toHaveBeenCalledOnce();
});

it('re-arms when a timer fires before the clock reaches the deadline', () => {
  vi.useFakeTimers();
  let clock = 0;
  const due = vi.fn();
  afterDeadline(10_000, due, () => clock);
  clock = 5_000; // the timer fires, but the clock says the wait is not over
  vi.advanceTimersByTime(10_000);
  expect(due).not.toHaveBeenCalled();
  clock = 10_000;
  vi.advanceTimersByTime(5_000);
  expect(due).toHaveBeenCalledOnce();
});

it('cancels', () => {
  vi.useFakeTimers();
  const due = vi.fn();
  const cancel = afterDeadline(Date.now() + 1_000, due);
  cancel();
  vi.advanceTimersByTime(2_000);
  expect(due).not.toHaveBeenCalled();
});
