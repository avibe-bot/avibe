import { afterEach, describe, expect, it, vi } from 'vitest';

import { createFontSizeStore } from './fontSizeStore';
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

afterEach(() => {
  _resetTerminalFontSize();
  vi.unstubAllGlobals();
});

describe('font size store factory', () => {
  it('uses each instance config and storage key', () => {
    const stored = new Map([['test.font.v2', '18.6']]);
    const localStorage = {
      getItem: vi.fn((key: string) => stored.get(key) ?? null),
      setItem: vi.fn((key: string, value: string) => stored.set(key, value)),
    };
    vi.stubGlobal('window', { localStorage });

    const store = createFontSizeStore('test.font.v2', { min: 10, max: 20, default: 12 });
    expect(store.get()).toBe(19);

    store.adjust(100);
    expect(store.get()).toBe(20);
    expect(localStorage.setItem).toHaveBeenLastCalledWith('test.font.v2', '20');
    expect(createFontSizeStore('test.font.v2', { min: 10, max: 20, default: 12 }).get()).toBe(20);
  });

  it('keeps updates and subscriptions working when storage is unavailable', () => {
    vi.stubGlobal('window', {
      localStorage: {
        getItem: () => {
          throw new Error('blocked');
        },
        setItem: () => {
          throw new Error('blocked');
        },
      },
    });
    const store = createFontSizeStore('test.blocked.v1', { min: 9, max: 24, default: 13 });
    const listener = vi.fn();
    store.subscribe(listener);

    store.adjust(1);
    expect(store.get()).toBe(14);
    expect(listener).toHaveBeenCalledWith(14);
  });
});

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
