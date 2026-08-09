import { describe, expect, it } from 'vitest';

import { canMarkConversationRead, isPageActive } from './pageActivity';

describe('isPageActive', () => {
  it('requires both a visible document and window focus', () => {
    expect(isPageActive({ visibilityState: 'visible', hasFocus: true })).toBe(true);
    expect(isPageActive({ visibilityState: 'visible', hasFocus: false })).toBe(false);
    expect(isPageActive({ visibilityState: 'hidden', hasFocus: true })).toBe(false);
  });
});

describe('canMarkConversationRead', () => {
  it('requires the visible chat transcript to be current and active', () => {
    expect(canMarkConversationRead({ pageActive: true, historicalWindow: false, showPageActive: false })).toBe(true);
    expect(canMarkConversationRead({ pageActive: false, historicalWindow: false, showPageActive: false })).toBe(false);
    expect(canMarkConversationRead({ pageActive: true, historicalWindow: true, showPageActive: false })).toBe(false);
    expect(canMarkConversationRead({ pageActive: true, historicalWindow: false, showPageActive: true })).toBe(false);
  });
});
