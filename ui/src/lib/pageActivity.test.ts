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
    const visibleTranscript = {
      pageActive: true,
      sessionReady: true,
      viewResolved: true,
      historicalWindow: false,
      showPageActive: false,
      foregroundAppWindow: false,
    };

    expect(canMarkConversationRead(visibleTranscript)).toBe(true);
    expect(canMarkConversationRead({ ...visibleTranscript, pageActive: false })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, sessionReady: false })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, viewResolved: false })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, historicalWindow: true })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, showPageActive: true })).toBe(false);
    expect(canMarkConversationRead({ ...visibleTranscript, foregroundAppWindow: true })).toBe(false);
  });
});
