# Preserve the GitHub Watch notification baseline

## Change contract

A combined PR/Actions Watch must not announce an already acknowledged CI result
again because a later query omitted runs, returned a nonterminal state, reordered
runs, or returned an older rerun attempt. A new PR head, run ID, rerun attempt, or
terminal conclusion remains reportable. PR review, comment, thread, and lifecycle
events remain independent of CI.

The existing `actions` cursor is the notification baseline, not a cache of the
latest poll. Preserve it until a complete, current-head terminal result is
reported. Reset it on a head transition. Keep its existing JSON shape and use
the existing staged-output/delivery-acknowledgement transaction; do not introduce
a second delivery owner, change reporting filters, or reseed existing Watches.

Retain the current head's known run inventory under an optional
`actions_observed` field, defaulting to the existing `actions` baseline for
version-1 files. Observation does not advance the notification baseline.
This is required by a consuming regression: a new failed run appears during
settle, disappears before delivery, and remains absent in later polls or across
a PR-only delivery/restart. Without that inventory, the very next poll can still
announce a success that omitted the known failed run. Reuse the existing
normalization, per-head lifetime, persistence, and pending transaction.

Incomplete observations are not new CI verdicts. In particular, a terminal subset
that omits a previously known run cannot turn an earlier failure into success.
Lower rerun attempts cannot supersede a delivered newer attempt. A settle
candidate that returns to the acknowledged baseline must not be reported.

### Report/snapshot atomicity

A settled report owns both its PR observation and its optional Actions verdict.
If a quiet re-poll omits previously detected PR activity, retain the PR
observation with that fallback output; derive its fingerprints, head, and
normalized snapshot from the same observation. Revalidate Actions separately
against the latest successful poll. Both still use the existing pending
transaction and delivery acknowledgement, not a second cursor or delivery owner.
The startup and loop paths must consume the same report/baseline contract.

Validate persisted Actions records before normalization or run-ID indexing.
Malformed records are not an empty inventory: fail closed with the existing
state-file diagnostic and preserve the file rather than discarding notification
history or silently disabling requested CI monitoring. An absent optional
observed inventory remains compatible with version-1 cursors.

## Boundaries

- Production changes are limited to the bundled GitHub PR waiter and stable
  Actions normalization, with a patch-version skill update.
- The Watch supervisor, GitHub authentication, retry budgets, polling intervals,
  PR review filters, and public CLI arguments are unchanged.
- Existing version-1 cursor files remain readable without manual migration.
- Histories already erased by older quiet-poll writes cannot be reconstructed;
  compatibility does not invent evidence of a past notification.
- The existing at-least-once contract remains: an unacknowledged staged report
  is replayed; an acknowledged report advances the same cursor.
- No service restart, merge, deployment, or production application change.

## Validation

Use hermetic synthetic GitHub responses, a virtual clock, and test-owned cursor
files. Reproduce the following before production edits, then verify the fix:

- complete -> missing/nonterminal/older attempt/partial -> same complete result;
- PR-only delivery while CI is absent, then acknowledgement and restart;
- a transient settle candidate that returns to the prior baseline.

Positive controls cover new heads, run IDs, rerun attempts, changed conclusions,
multi-workflow gates, and undelivered replay. Run the complete PR/Actions waiter
tests, related Watch supervisor tests, skill guidance tests, and changed-file lint.
Obtain independent read-only review and exact-head GitHub review/CI before
delivery close-out. Existing live Watches retain their IDs, cursors, and filters.

Settle tests must continue through unacknowledged replay and acknowledged
restart, including restoration of the unchanged PR item and a subsequent real
edit/deletion. Cover PR-only and combined reports, startup and loop detection,
and malformed committed/observed Actions records without overwriting evidence.

Explicit PR replay baselines unrelated CI at the currently fetched head without
altering the requested PR history. SHA comparisons use the same case-insensitive
contract as preflight and run selection. An explicit ownerless seed writes the
current CI baseline and inventory; this supported CLI operation is distinct from
ordinary Watch re-arming, which must never reseed.

## Known by design

- Missing or regressed Actions observations do not establish a fresh terminal
  gate. A deleted known run can require investigation; absence is not success.
- This fixes a reproduced notification defect. Historical raw polling responses
  were not retained, so it does not claim to prove every earlier duplicate's
  remote API cause.
- Source delivery does not automatically install a new Avibe release.
