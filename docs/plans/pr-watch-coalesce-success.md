# Coalesce successful PR review and CI notifications

## Change contract

The bundled combined PR/CI waiter gains an opt-in `--coalesce-success` reporting policy. The preferred PR delivery example enables it. PR-only and standalone Actions monitoring keep their existing behavior.

Observable invariants:

- A current-head CI success alone or a recognized current-head Codex clean-pass alone does not emit a report or create an Agent turn.
- Once both are present for the same head, emit one combined success candidate. The follow-up Agent still verifies all delivery/merge gates; the waiter does not authorize a merge.
- New CI failures report even if another requested workflow is missing or running. Review findings, Codex infrastructure errors, lifecycle/head changes, and unfamiliar review payloads remain reportable.
- Preserve successful review evidence while CI is pending, including across a restart and a problem-only report. Invalidate it on a new head, changed/deleted evidence, or new bot findings/errors.
- Never combine an old-head pass with new-head CI. A PR-body reaction is only a completion candidate when this durable observer first sees it after the unchanged head's baseline. Existing or head-transition reactions are not manufactured into head-bound passes.
- Keep the original owner-specific state file, pending transaction, at-least-once replay, and delivery acknowledgement. Reporting policy participates in cursor ownership. Existing activity-mode Watches remain unchanged; enabling a different policy requires a separately established pre-action baseline, not reseeding an armed Watch.
- Recognized SHA-bearing clean comments/reviews and newly observed Codex PR-body `+1` reactions are the supported success shapes. Unknown/ambiguous evidence is surfaced for judgment, not silently accepted as a merge gate.

## Observation and delivery invariants

Success batching must track monotonic observations, not replaceable response snapshots. The current head retains the latest ordered bot outcome, including an invalidating error/finding; a later clean verdict in the same batch supersedes an earlier error, but restoring an older row cannot supersede a newer outcome. Compare semantic evidence, not timestamps or user-profile metadata. Keep a reaction-ID high-water mark across head changes, initialized from the durable reaction cursor. Explicit unbound reaction replay remains ordinary activity.

Advance those observations on every successful poll and settle fetch. Rendering a candidate must not commit delivery progress; the existing pending transaction still owns acknowledgement. Bind the report's PR head and deferred batch to the same observation, including transient A-to-B-to-A fetch sequences.

Fail-fast CI may tolerate missing inventory, never an older run attempt. Merge acknowledged partial failure inventories instead of replacing previously notified run IDs. Preserve the existing complete-inventory requirement for a success candidate.

Malformed batching state fails only the affected waiter, with the original evidence intact and the supervisor's existing error notification. It is deferred delivery/notification history, not disposable optional-feature configuration. Clearing it can lose the only review evidence or notification watermark. This does not affect Avibe service startup; see the same distinction for Actions history in `docs/plans/watch-actions-dedup.md`.

## Validation evidence

- With only the new parser flag present, both arrival-order regressions failed because the unchanged waiter emitted a single success (`rc=0` instead of the expected quiet wait).
- The focused PR waiter, Actions waiter, repository guidance, and Harness Skill guidance suite passes after the full observation-history repair: **339 tests**. The related CLI Watch tests also passed in the earlier combined run; that run's only failure was the newly added documentation-command extractor, corrected and revalidated by the focused suite.
- Changed-file Ruff, `git diff --check`, and `askill validate skills/background-watch-hook/SKILL.md` pass.
- The normal Codex summary table shape was checked against a live read-only PR response. Its normal Running/Completed updates are quiet; failure and unrecognized statuses remain visible.
- First-head independent review found ordered-outcome, reaction-epoch, settle, replay, and partial-CI-history regressions. Fourteen focused cases were run against the original `3c88ae3d1` waiter and failed for those intended behavioral reasons, including metadata-only invalidation. They now exercise the monotonic-observation contract rather than a collection of special-case retries.
- GitHub first-head findings on exact summary statuses and seeded stale passes are covered by suffix-status and initialization ordering regressions. The malformed-history suggestion is intentionally not adopted for the delivery-state reason above.
- Independent read-only re-review, current-head GitHub Codex review, and required CI remain delivery gates, not waived by these local results.

## Known by design

- Activity mode is the compatibility default; the preferred delivery example explicitly enables coalescing on both the seed and waiter. This is not an in-place migration of an already armed Watch's reporting identity.
- The combined success is a candidate, not merge permission. The Agent still checks exact-head review qualification (especially reaction epochs), all required CI, and all unresolved threads.
- Head, lifecycle, thread, human-request, and unknown-result activity can still wake the Agent; the notification reduction is deliberately limited to recognized green/progress payloads.
- Error payloads use the existing bounded rendering and link back to the full GitHub object, which the follow-up fetches.
- No existing Watch was replaced, no service was restarted, and no installed built-in Skill snapshot was upgraded.
- Fail-closed notification-history corruption is intentional; it stops the affected waiter and sends the existing terminal failure notice, not Avibe startup. Do not silently discard deferred evidence or an undelivered transaction.

## Owning boundaries and scope

Implementation belongs in `skills/background-watch-hook/scripts/wait_pr.py` and its existing shared Actions renderer. Document the option and bump the Skill minor version. Do not change the Watch supervisor, service configuration, polling/retry budgets, merge policy, or unrelated live Watches. No service restart, deployment, automatic merge, or registry publication is part of this change.

## Validation contract

The existing managed-cycle harness provides synthetic GitHub snapshots, a virtual clock, and test-owned files. Extend it rather than introducing a second framework. Existing tests intentionally report each success independently and do not cover this new policy.

Add owner-boundary regressions for both arrival orders; quiet half-success across restart; paired notification plus undelivered replay and acknowledged deduplication; SHA/comment/review/reaction shapes; stale or ambiguous evidence; immediate partial-CI failure; bot findings/infrastructure failure before the other gate; evidence revocation; and head changes. A pre-implementation run with only the parser flag present must fail because it reports a single success, not because the option is missing.

Run focused PR/Actions waiter tests, related repository guidance and Watch tests, changed-file Ruff, and Skill validation. Obtain exact-head GitHub Codex review, complete required CI, and zero unresolved threads before close-out. A source PR does not upgrade the user's installed Skill snapshot; existing live monitoring remains on its configured package until an authorized rollout.
