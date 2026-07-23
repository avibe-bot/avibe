import { describe, expect, it } from 'vitest';

import { removeSessionRow, type ProjectSessionsState } from './WorkbenchProjectsContext';
import type { WorkbenchSession } from './ApiContext';

// removeSessionRow only reads `.id`; a minimal cast keeps the fixtures readable.
const sess = (id: string) => ({ id }) as WorkbenchSession;

function loaded(...ids: string[]): ProjectSessionsState {
  return { sessions: ids.map(sess), loading: false, loadingMore: false, cursor: null, error: false };
}

// removeSessionRow is the optimistic local drop shared by the archive path, the
// A6 session.activity SSE listener, and (new) the Hide-to-background PATCH-response
// reconcile. It must drop the row without trusting the SSE event, and stay
// referentially stable when nothing changed so unrelated consumers don't re-render.
describe('removeSessionRow (optimistic hide/archive drop)', () => {
  it('drops the row from the project that holds it, leaving siblings untouched', () => {
    const prev = { p1: loaded('a', 'b'), p2: loaded('c') };
    const next = removeSessionRow(prev, 'b');
    expect(next.p1.sessions?.map((s) => s.id)).toEqual(['a']);
    // The unaffected project keeps its exact object identity (no re-render).
    expect(next.p2).toBe(prev.p2);
  });

  it('returns the SAME state object when no row matches (so consumers do not re-render)', () => {
    const prev = { p1: loaded('a', 'b') };
    expect(removeSessionRow(prev, 'missing')).toBe(prev);
  });

  it('leaves a not-yet-loaded project (sessions === null) alone without throwing', () => {
    const prev = {
      p1: { sessions: null, loading: false, loadingMore: false, cursor: null, error: false } as ProjectSessionsState,
    };
    expect(removeSessionRow(prev, 'x')).toBe(prev);
  });
});
