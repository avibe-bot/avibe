import { describe, expect, it } from 'vitest';

import { visibilityActivityEvents } from './sessionVisibilityEvents';

describe('visibilityActivityEvents (replay a visibility PATCH as A6 session.activity events)', () => {
  it('hide → updated(visibility=background) + user_message placement on the same scope', () => {
    expect(
      visibilityActivityEvents({ sessionId: 'ses1', scopeId: 'scope1', title: 'T', visibility: 'background' }),
    ).toEqual([
      { session_id: 'ses1', scope_id: 'scope1', event: 'updated', title: 'T', visibility: 'background' },
      { session_id: 'ses1', scope_id: 'scope1', event: 'user_message' },
    ]);
  });

  it('undo/restore → updated(visibility=foreground) + a `created` placement (tree reconciles it with minCount 1)', () => {
    expect(
      visibilityActivityEvents({ sessionId: 'ses1', scopeId: 'scope1', title: null, visibility: 'foreground' }),
    ).toEqual([
      { session_id: 'ses1', scope_id: 'scope1', event: 'updated', title: null, visibility: 'foreground' },
      { session_id: 'ses1', scope_id: 'scope1', event: 'created' },
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
