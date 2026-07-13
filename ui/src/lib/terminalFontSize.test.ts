import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  TERMINAL_FONT_DEFAULT,
  TERMINAL_FONT_MAX,
  TERMINAL_FONT_MIN,
  adjustTerminalFontSize,
  getTerminalFontSize,
  resetTerminalFontSize,
  subscribeTerminalFontSize,
  _resetTerminalFontSize,
} from './terminalFontSize';

afterEach(() => _resetTerminalFontSize());

describe('terminal font size preference', () => {
  it('starts at the default size', () => {
    expect(getTerminalFontSize()).toBe(TERMINAL_FONT_DEFAULT);
  });

  it('adjusts up and down by whole steps', () => {
    adjustTerminalFontSize(1);
    expect(getTerminalFontSize()).toBe(TERMINAL_FONT_DEFAULT + 1);
    adjustTerminalFontSize(-2);
    expect(getTerminalFontSize()).toBe(TERMINAL_FONT_DEFAULT - 1);
  });

  it('clamps to the [MIN, MAX] bounds instead of running away', () => {
    adjustTerminalFontSize(100);
    expect(getTerminalFontSize()).toBe(TERMINAL_FONT_MAX);
    adjustTerminalFontSize(-100);
    expect(getTerminalFontSize()).toBe(TERMINAL_FONT_MIN);
  });

  it('resets to the default', () => {
    adjustTerminalFontSize(5);
    resetTerminalFontSize();
    expect(getTerminalFontSize()).toBe(TERMINAL_FONT_DEFAULT);
  });

  it('notifies subscribers so every open terminal re-fits to the new size', () => {
    const seen: number[] = [];
    const unsubscribe = subscribeTerminalFontSize((size) => seen.push(size));
    adjustTerminalFontSize(1);
    adjustTerminalFontSize(1);
    unsubscribe();
    adjustTerminalFontSize(1); // no longer listening
    expect(seen).toEqual([TERMINAL_FONT_DEFAULT + 1, TERMINAL_FONT_DEFAULT + 2]);
  });

  it('does not notify when a change is a no-op at a bound', () => {
    adjustTerminalFontSize(100); // pin to MAX
    const listener = vi.fn();
    subscribeTerminalFontSize(listener);
    adjustTerminalFontSize(1); // already at MAX → clamped to the same value
    expect(listener).not.toHaveBeenCalled();
  });
});
