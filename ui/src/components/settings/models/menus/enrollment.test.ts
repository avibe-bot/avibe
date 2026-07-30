// api.md → "Mapping and menu enrollment": accepting a target auto-appends its
// supplier to the backend's order, and 「the confirm step must surface the
// append」. These pin the one rule that makes the notice safe to show — the
// append is READ, never predicted — and the two silences that must not become a
// sentence.
import { describe, expect, it } from 'vitest';

import { enrolledByCommit } from './enrollment';
import type { AgentSupply } from '../types';

const withOrder = (order: string[] | null): Pick<AgentSupply, 'sources'> => ({
  sources: order === null ? null : { policy: 'custom', order, eligibility: null },
});

describe('enrolledByCommit', () => {
  it('names what the commit added, and nothing that was already there', () => {
    expect(enrolledByCommit(withOrder(['src_a']), withOrder(['src_a', 'src_b']))).toEqual(['src_b']);
  });

  it('says nothing when the order came back unchanged', () => {
    // The ordinary case: the ticked model's supplier was already enrolled, so the
    // save changed the menu and nothing else. A notice here would fire on every
    // 完成 and teach the user to dismiss the one that matters.
    expect(enrolledByCommit(withOrder(['src_a', 'src_b']), withOrder(['src_a', 'src_b']))).toEqual([]);
  });

  it('reports several appends in the order the server put them', () => {
    // One per target group, and the contract caps each group at one — but a
    // multi-target commit can still enroll more than one supplier.
    expect(enrolledByCommit(withOrder([]), withOrder(['src_b', 'src_c']))).toEqual(['src_b', 'src_c']);
  });

  it('reads a missing baseline as nothing to report, never as everything new', () => {
    // `sources` is null in direct mode, and a drawer that opened against one
    // would otherwise announce the entire order as freshly enrolled — the one
    // sentence this must never produce.
    expect(enrolledByCommit(withOrder(null), withOrder(['src_a', 'src_b']))).toEqual([]);
  });

  it('claims nothing when the echo itself carries no order', () => {
    expect(enrolledByCommit(withOrder(['src_a']), withOrder(null))).toEqual([]);
  });

  it('ignores a reordering that enrolled nobody', () => {
    // The diff is membership, not position: the drawer does not edit the order,
    // so a server that re-sorted it has still enrolled nothing.
    expect(enrolledByCommit(withOrder(['src_a', 'src_b']), withOrder(['src_b', 'src_a']))).toEqual([]);
  });

  it('does not report a source the commit REMOVED as if it were added', () => {
    // A dropped id (deleted, or newly ineligible) is a shrinking order. The
    // notice is about what came in; a subtraction has no sentence here.
    expect(enrolledByCommit(withOrder(['src_a', 'src_b']), withOrder(['src_a']))).toEqual([]);
  });
});
