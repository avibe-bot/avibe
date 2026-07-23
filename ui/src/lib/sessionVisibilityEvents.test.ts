import { describe, expect, it } from 'vitest';

import { createdReconcileMinCount, visibilityActivityEvents } from './sessionVisibilityEvents';

describe('visibilityActivityEvents (replay a visibility PATCH as A6 session.activity events)', () => {
  it('hide → updated(visibility=background) + user_message placement on the same scope', () => {
    expect(
      visibilityActivityEvents({ sessionId: 'ses1', scopeId: 'scope1', title: 'T', visibility: 'background' }),
    ).toEqual([
      { session_id: 'ses1', scope_id: 'scope1', event: 'updated', title: 'T', visibility: 'background' },
      { session_id: 'ses1', scope_id: 'scope1', event: 'user_message' },
    ]);
  });

  it('undo/restore → updated(visibility=foreground) + a `created` placement marked `restored`', () => {
    expect(
      visibilityActivityEvents({ sessionId: 'ses1', scopeId: 'scope1', title: null, visibility: 'foreground' }),
    ).toEqual([
      { session_id: 'ses1', scope_id: 'scope1', event: 'updated', title: null, visibility: 'foreground' },
      // `restored: true` is the marker a real backend `created` never carries; it
      // lets the tree grow its window to bring a restored row back.
      { session_id: 'ses1', scope_id: 'scope1', event: 'created', restored: true },
    ]);
  });

  it('carries visibility only on the updated event (the Inbox driver) and passes a null scope through', () => {
    const [updated, placement] = visibilityActivityEvents({
      sessionId: 's',
      scopeId: null,
      title: null,
      visibility: 'background',
    });
    expect(updated).toMatchObject({ event: 'updated', visibility: 'background', scope_id: null });
    // The placement event is a REORDER event with no visibility, so the Inbox
    // listener ignores it and only the projects-tree listener reconciles.
    expect(placement.visibility).toBeUndefined();
  });
});

describe('createdReconcileMinCount (marker gates window growth)', () => {
  it('a real `created` (unmarked new session) keeps minCount 1 → no window inflation', () => {
    // targetCount = max(loaded, 1) = loaded, so repeated local create/fork (which
    // already prepend the row) do NOT grow the loaded window each time.
    expect(createdReconcileMinCount(false, 8)).toBe(1);
    expect(createdReconcileMinCount(false, 0)).toBe(1);
  });

  it('a synthesized restore (`restored`) fetches one past the window → row past the page returns', () => {
    // targetCount = max(loaded, loaded + 1) = loaded + 1, so a restored row ranked
    // just past the loaded page is included even at a page boundary.
    expect(createdReconcileMinCount(true, 8)).toBe(9);
    expect(createdReconcileMinCount(true, 0)).toBe(1);
  });
});
