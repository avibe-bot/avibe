import { describe, expect, it } from 'vitest';

import {
  canMarkConversationRead,
  createPageActivityTracker,
  isPageActive,
  type PageActivitySnapshot,
} from './pageActivity';

describe('isPageActive', () => {
  it('requires both a visible document and window focus', () => {
    expect(isPageActive({ visibilityState: 'visible', hasFocus: true })).toBe(true);
    expect(isPageActive({ visibilityState: 'visible', hasFocus: false })).toBe(false);
    expect(isPageActive({ visibilityState: 'hidden', hasFocus: true })).toBe(false);
  });
});

describe('createPageActivityTracker', () => {
  const observeAll = (initial: boolean, snapshots: PageActivitySnapshot[]): boolean[] => {
    const tracker = createPageActivityTracker(initial);
    return snapshots.map((snapshot) => tracker.observe(isPageActive(snapshot)));
  };

  it('reports the inactive -> active edge once per gap', () => {
    expect(
      observeAll(true, [
        { visibilityState: 'hidden', hasFocus: false },
        { visibilityState: 'visible', hasFocus: true },
        { visibilityState: 'visible', hasFocus: true },
      ]),
    ).toEqual([false, true, false]);
  });

  it('reports a return of window focus without a visibility change', () => {
    expect(
      observeAll(true, [
        { visibilityState: 'visible', hasFocus: false },
        { visibilityState: 'visible', hasFocus: true },
      ]),
    ).toEqual([false, true]);
  });

  it('waits for focus when a revealed tab is not focused yet', () => {
    expect(
      observeAll(true, [
        { visibilityState: 'hidden', hasFocus: false },
        { visibilityState: 'visible', hasFocus: false },
        { visibilityState: 'visible', hasFocus: true },
      ]),
    ).toEqual([false, false, true]);
  });

  it('stays silent while focus moves inside a page that never left', () => {
    // Focusing an embedded Show Page iframe blurs the parent window, but the
    // parent document stays visible and keeps `document.hasFocus()`.
    expect(
      observeAll(true, [
        { visibilityState: 'visible', hasFocus: true },
        { visibilityState: 'visible', hasFocus: true },
      ]),
    ).toEqual([false, false]);
  });

  it('does not treat a first active reading as a return when it starts active', () => {
    const tracker = createPageActivityTracker(true);
    expect(tracker.isActive()).toBe(true);
    expect(tracker.observe(true)).toBe(false);
  });

  it('treats becoming active for the first time as a return when it starts inactive', () => {
    const tracker = createPageActivityTracker(false);
    expect(tracker.observe(true)).toBe(true);
    expect(tracker.isActive()).toBe(true);
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
