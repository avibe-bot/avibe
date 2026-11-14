# Coordinated epoch-3 writer activation (3.2.7)

## Status and release boundary

The cache, Vault snapshot, and Skill catalog readers and the reader-admission
bridge shipped in `v3.2.6` (`7fc2576a75671ad4a0d2135c4563def48436b0e9`).
On November 14 Product authorized continuation for 3.2.7. The existing isolated
worktree was fast-forwarded to freshly fetched master `96311f7f4`; the Vault
reader hardening and coherent delivery-capture fix are now included. This plan
is not an activation, merge authorization, release, or operational change.

The activation must not enter the 3.2.6 release. A published reader-capable
predecessor and its actual wheel contract are required before claiming rollback
support. Source fixtures and merged reader PRs alone are insufficient evidence.
The lead owns release sequencing, merge and the serialized Incus slot.
Product requires an actual upgrade of populated state created by the published
3.2.6 binary, followed by Product's go on the exact reviewed candidate head
before the lead merges. The Incus slot has been requested; it is not yet granted.
No owner live database is required or authorized for that exercise.

## Invariants

- One coordinated epoch-3 breaking revision advances marker and stamp in the
  migration transaction, with the existing conservative upgrade snapshot.
- Cache state, Vault audit references and Skill catalog references activate at
  the same boundary; below it, preserve existing writer bytes.
- No old conversation or audit row rewrite, backfill, deletion for savings,
  VACUUM, signature slimming, or change to approved dashboard refresh behavior.
- Every Vault request retains its individual audit event and exact timestamp,
  requester, resource, outcome and delivery facts. Non-proxied writes retain
  their original representation. Resolve immutable historical access evidence
  before ACL evaluation; missing/corrupt references fail closed.
- Cache state and the originating response/attempt/side-turn commit atomically;
  an older attempt cannot replace a newer state. Historical forks never inherit
  mutable parent future state. Derived diagnostics remain inline.
- Skill catalog/event/reference ownership/daily counters commit atomically.
  Duplicate receipts neither change event bytes nor increment counters.
  Last-reference cleanup respects shared catalogs, 90-day raw retention,
  365-day daily retention, explicit clear and Session/scope deletion.
- Added ownership and cleanup work is index-keyed and bounded. No per-request
  historical scans. Rollback to the reader bridge, its inline writes and its
  existing delete paths must not leave dangling references or unbounded stale
  ownership when the new writer returns.

## Settled integration design (November 14)

- Skill ownership uses a partial expression index on existing `agent_events`
  plus last-reference DELETE/UPDATE triggers. This avoids a new table exposed
  by the unchanged 3.2.6 SQL denylist and covers old clear/retention/delete paths.
  Building the index scans events once during the breaking migration; no event
  payload is rewritten. Runtime cleanup is digest-keyed, including preexisting
  reference rows. Arbitrary `INSERT OR REPLACE` is not a supported event writer.
- Cache teardown is also database-owned (Session archive/delete triggers), so
  the bridge's old teardown code cannot strand state. New cache writes commit
  with their response or audit, never in loop code. Valid route snapshots in a
  newly written audit are carried into state before removing repeated inline
  `cache_state`: otherwise the latest-audit reader loses previously restored
  routes when the first compact audit hides its legacy predecessor. All these
  upserts use the reader's timestamp/prefix tie rule and skip unchanged values;
  they inspect only the incoming payload, never scan/backfill old history.
- Vault interning preserves the complete captured snapshot and allocates IDs
  from a migration-initialized durable state_meta high-water key. Missing,
  malformed or exhausted allocation state fails the write; no Vault GC.
- One proof-plus-epoch gate activates all three. The source proof is now 3
  after the actual published 3.2.6 wheel passed the extended contract (below).
  Epoch-3 tool projection suppression also requires that proof, without
  disabling the epoch-2 representation supported by those same readers.
- A lead-owned exclusive Incus window is required on an isolated worktree
  target. Confirm the exact candidate head before starting and delete the
  temporary target after the exercise; do not overwrite the shared master.

## Remaining integration work

1. Low-disk admission is implemented at the shared writable preflight,
   importer, factory and standalone backup boundaries. Read-only admission and
   a separately budgeted crash-recovery path preserve cold WAL and committed
   revision semantics. Budgets are conservative policy, never measured minima;
   clone success, pruning and backup reuse are not credited.
2. Complete focused consumer/lifecycle coverage and verify historical fixture
   reconstruction refuses real compact data. Production downgrade guards remain
   unchanged; 3.2.6 rollback is compatible-open, never restamping.
3. The phased measurement harness and [procedure](../maintenance/epoch3-measurements.md)
   are implemented. Execute them only in the approved isolated Incus target;
   large-upgrade timing, space and per-writer benefit remain unmeasured.
4. Recheck the migration number against default branch before opening the PR.
   The current draft is `20261114_0089` on `20261112_0088`.
5. Obtain exact-head code/security review and required CI; run the populated
   Incus acceptance, then Product exact-head go and lead-owned merge.

## Evidence and remaining acceptance

Low-disk coverage additionally passes 182 focused admission/backup/preflight/
readiness tests and 172 importer/startup/no-copy consumers. The new cases include
existing backup/lifecycle preservation, crash-left WAL without a live keeper,
hot-journal refusal before recovery, read-only callers, and honest diagnostics
when capacity falls after completed recovery. Sixteen tiny harness tests exercise
real store APIs with inline/compact gates, subprocess isolation, desktop refusal
and a small message-shape fixture. These counts are local focused runs, not a
full-suite or large-database result.

On November 14 the extended predecessor harness installed the actual published
`avibe_os-3.2.6-py3-none-any.whl`, SHA256
`59fa0184ecfd97fffa77dc74c99b2560284c0a9fad4cd2fccdb5616a09569197`,
in its own interpreter and HOME/XDG tree. Version/package-path verification,
candidate compact writes, released readers/historical ACL filtering, duplicate
receipt, old writes and Skill clear, candidate return, canonical tool projection,
unknown-column preservation and unsupported-epoch refusal passed. The harness
set `VERIFIED_PREDECESSOR_EPOCH` to 3 only after this success. Source-fixture
negative controls additionally remove each of the three referenced-state groups
and require the corresponding reader probe to fail. This is small synthetic
state, not the required large Incus upgrade or a performance result.

An early independent read-only security/correctness review found no actionable
defects in the eight writer/migration files and confirmed the indexed cleanup
query plan using small isolated probes. It is not the final exact-head PR review.

Product added three delivery gates on November 14:

- In the lead's isolated Incus target, synthesize populated 3.2.6 state of about
  4.5 GB and test a nearly-full filesystem, recording actual available bytes and
  capacity (91% alone is not a capacity). Measure backup plus upgrade elapsed
  time, peak additional space and the conservative free-space admission budget.
  Insufficient space must refuse before DB/checkpoint/backup/prune changes and
  say how much to free. The preflight implementation is still pending.
- Measure each writer's before/after growth with the same deterministic workload
  and report per 1,000 turns/requests and clearly justified daily extrapolation.
  Include index/snapshot/state cost, not just shortened JSON; do not promise
  immediate shrinkage. Product may remove a low-benefit group after seeing data.
- Provide short release notes covering unchanged normal use, measured downtime,
  verified 3.2.6 rollback and refusal/recovery for unsupported older readers.

No large database generation, disk filling or load work runs on the owner's Mac.
No production database or credentials are used. Incus, Product exact-head go,
code/security review, CI, merge and release gates remain outstanding.

## Non-goals

No service restart, live database access, credential operation, remote tenant
operation, load/stress experiment, release tag, or deployment is authorized by
this implementation plan. Incus verification remains serialized by the lead.
