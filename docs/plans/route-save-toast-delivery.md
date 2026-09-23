# Route save toast: reconciliation scope decision

## Review inventory

The delivery owner inspected all 17 review threads, including resolved and
outdated threads, and the current diff and consuming tests before continuing
from `b86dce2b7`.

| Findings-bearing head | Root-cause classes |
| --- | --- |
| `aa80bc4ecd` | Browser-test and specification alignment; success announcement; post-install focus |
| `e3e0ad7a2c` | Focus ownership/lifetime; coalesced toast lifetime |
| `69347353a1` | Focus ownership/lifetime; remaining modal specification; commit-evidence lifetime |
| `36bac711dd` | Commit-evidence lifetime across consecutive saves |
| `45a049bb5c` | Inferred-commit focus ownership; failed reconciliation blocking subsequent saves; Retry admission |
| `31043043a1` | Retained reconciliation failure hidden by a later unrelated region refresh |
| `b86dce2b7a` | Successful member settlement published too late; queued commit excluded from focus ownership |

There are seven findings-bearing heads without a clean pass. Focus and evidence
ownership recur across heads, so the review-loop circuit breaker applies.
Passing CI on `45a049bb5c` does not close the review gate.

## Diagnosis and bounded decision

The editor is no longer the lifetime owner after commit. The page must retain
each exact response and its original focus context independently of the next
editor. Likewise, waiting for a failed read to be retried cannot occupy the
single in-flight collection-read slot indefinitely.

Keep page-owned reconciliation and the existing collection authorities. Serialize
in-flight reads, but carry failed reports into the next new commit's collection
refresh. Those later Agents/Sources reads are acquired after both commit frontiers
and can settle both reports without reconstructing either response tail. A report
committed during an in-flight read waits for a subsequent read, not the older one.
Retry still reads only the failed subset and its newly enabled dependants.

Retain the original opener with the suspended attempt, then pass it explicitly
when commit is inferred. Use the committed chain's backend/model identity for
fallback, never the currently open editor. Move focus before disabling a page
Retry and keep its handler owned by reconciliation while busy.

An independent local review identified the same admission issue on an automatic
start: a suspended commit can be inferred while the user focuses another
commit's failed Retry. The owner reproduced it in the consuming page test.
Focus handoff therefore belongs at every M6 pending transition, not just the
Retry click handler. Only the registered focused Retry is moved; other valid
user focus remains untouched.
The independent re-review of that correction found no further actionable defect.

This is a reversible UI-lifecycle correction. It changes neither API contracts,
mutation admission nor the requested immediate close with the shared saved toast.
Do not introduce a second failure stack or resurrect stale failure banners after
a newer M6 collection has settled those obligations successfully.

## Cross-refresh visibility decision

The sixth reviewed head exposed a rendering boundary, not a missing retry in the
queue. The M6 obligation and the latest region observation have different
lifetimes: a Source echo, presence refresh, or another mutation's full read may
replace a region with ready data without settling the retained route report.
The shared card rendered only the region's failure and could hide the sole M6
Retry while its backend remained excluded from chain probes.

Keep the underlying authoritative region reads unchanged. Compose the retained
M6 failed subset into the reads used to select the page form and render its
Sources/Gateway cards, reusing the existing stale-data and Retry surfaces. This
keeps newly read data visible and Retry available even after an unrelated ready
or refreshing projection; it does not certify an entity echo as a full,
frontier-satisfying collection read. Include the direct/empty page-form edge so
it cannot hide the failure surface either. Only M6 settlement clears its failure
and releases the held reports and chain-probe exclusion.

Integrate upstream source-toast PR #2121 without restoring either success
modal. Preserve its per-chain read ownership and Source-dialog return focus
while retaining this PR's per-report reconciliation and route focus ownership.
The two merge conflicts are in the page and workflow specification; the scope
remains a reversible UI correction with unchanged mutation/API contracts.

## Member settlement and queued focus decision

The seventh reviewed head exposes two remaining publication boundaries. The
reader already installs Agents before acquiring Sources, but its failure
snapshot is published only at batch completion. The receiving page therefore
keeps a cleared failure visible until an independent request finishes. Publish
the removal of an acquired Agents failure immediately after its successful
installation; keep the batch pending, other failures and held reports intact.

The published single report is used for focus, whereas the report list is used
for batch settlement and cleanup. Their lifetimes are not identical: save B
becomes the latest focus owner immediately, even while its reads wait for A.
Publish the newest outstanding report, including the queued tail, as the focus
identity. Keep the batch's report list unchanged so A cannot settle or release
B's evidence. Both asynchronous refocus and pre-disable Retry handoff already
consume this identity; preserve valid user-moved focus in either case.

The owner verified these consumers and the existing queued-report and page
focus tests before choosing this scope. Neither finding requires new queue
state, a second focus registry, or changed API/mutation contracts. Regressions
must inspect the intermediate Agents-success/Sources-pending state and the
older-install/newer-opener-removal state, not only final batch completion.

Independent review found one more boundary in the same focus class: the page
captured that identity before scheduling an animation frame. A newer inferred
commit between installation and frame execution could still focus the older
route, after which preservation of valid focus made the mistake permanent.
The owner reproduced that exact interleaving in a consuming-page test. Resolve
the identity and opener again when the frame executes, without merely skipping
the old callback: the newer batch may still be waiting. Retain the captured
opener only for the same report after its settlement cleanup, and continue
preserving valid user-moved focus. This extends the same bounded publication
fix to its final consumer; report settlement and read ownership remain unchanged.

## Validation contract

Run tests and builds from the task worktree's `ui/`, not the control checkout.
Cover both Agents and Sources failures, new commits arriving before and after a
failure, repeated Retry activation, exact response-tail retention, and suspended
inference with another editor open. Existing focus-preservation and automatic
close browser scenarios must continue to pass.

## Local verification at `31043043a`

- The affected Model Hub and shared toast suite passes all 1,439 tests across
  85 files with two workers. An initial higher-concurrency run timed out in one
  unchanged API-key validation test; its isolated file and the complete rerun
  both passed without changing the test or its timeout.
- All 150 model-catalog browser cases pass, including desktop/mobile,
  English/Chinese, and the automatic route-dialog close flows.
- The production build, test type checks, lint-baseline gate, and whitespace
  checks pass from this task worktree.
- Local checks do not replace the new exact-head CI and automatic review gates.

## Local verification after the cross-refresh fix and base integration

- All four consuming-page regressions fail without the display composition and
  pass with it: Agents/Sources failure crossed with normal/direct-empty landings.
  They cover the intermediate Source echo, newly observed data, failed-subset
  Retry, one route PUT, and release of the retained backend's chain probes.
- The affected Model Hub, shared toast, and settings layout suite passes all
  1,494 tests across 85 files with two workers. The four regressions also pass
  after adding the intermediate echo assertion.
- All 150 model-catalog and eight model-provider browser cases pass.
- The production build, test type checks, lint-baseline gate, and whitespace
  checks pass from this task worktree.
- Independent read-only review compared the integrated tree against both
  parents and found no actionable defect or semantic merge regression. It
  confirmed M6 ownership and focus behavior, the direct-empty branch, and
  preservation of the upstream Source toast and per-chain read authority.
- The new pushed head still requires its own Actions and automatic review;
  prior-head results and local verification do not close those gates.

## Local verification after the member/focus publication fix

- Four new regression cases fail against the previous production code: two
  intermediate Agents-recovery cases with and without an older Sources failure,
  immediate Direct landing while Sources is pending, and a queued save losing
  its opener to the older read. The user-moved-focus control passes unchanged.
- The added same-frame inferred-commit regression also fails before frame-time
  identity lookup and passes after it; its user-moved-focus control passes in
  both versions. All seven added cases pass with the complete fix.
  The existing queue test also confirms
  that the newest queued focus identity does not enter the older batch's
  settlement/cleanup report list.
- All 96 targeted reconciliation/page/focus tests and the full 1,501-test
  Model Hub/toast/settings-layout suite (85 files, two workers) pass.
- All 150 model-catalog desktop/mobile browser cases pass. Production build,
  test type checks, lint-baseline gate and whitespace checks pass from this
  task worktree.
- Independent review of the complete six-file increment found no further
  actionable defect and separately passed 36 focused tests, including both
  frame-execution regressions. No queue or API redesign is needed.
