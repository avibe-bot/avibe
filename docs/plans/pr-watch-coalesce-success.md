# Coalesce successful PR review and CI notifications

## Change contract

The bundled combined PR/CI waiter gains an opt-in `--coalesce-success` reporting policy. The preferred PR delivery example enables it. PR-only and standalone Actions monitoring keep their existing behavior.

Observable invariants:

- A current-head CI success alone or a recognized current-head Codex clean-pass alone does not emit a report or create an Agent turn.
- Once both are present for the same head, emit one combined success candidate. Bind the candidate and notification signature to that head, the selected semantic review evidence, and the CI verdict inventory; stronger review evidence must wake the Agent even when CI is unchanged. The follow-up Agent still verifies all delivery/merge gates; the waiter does not authorize a merge.
- New CI failures report even if another requested workflow is missing or running. Review findings, Codex infrastructure errors, lifecycle/head changes, and unfamiliar review payloads remain reportable.
- Preserve successful review evidence while CI is pending, including across a restart and a problem-only report. Invalidate it on a new head, changed/deleted evidence, or new bot findings/errors.
- Never combine an old-head pass with new-head CI. A PR-body reaction is only a completion candidate when this durable observer first sees it after the unchanged head's baseline. Existing or head-transition reactions are not manufactured into head-bound passes.
- Keep the original owner-specific state file, pending transaction, at-least-once replay, and delivery acknowledgement. Reporting policy participates in cursor ownership. Existing activity-mode Watches remain unchanged; enabling a different policy requires a separately established pre-action baseline, not reseeding an armed Watch.
- Recognized SHA-bearing clean comments/reviews and newly observed Codex PR-body `+1` reactions are the supported success shapes. Unknown/ambiguous evidence is surfaced for judgment, not silently accepted as a merge gate.

## Observation and delivery invariants

Success batching must track monotonic observations, not replaceable response snapshots. The current head retains the latest ordered bot outcome, including an invalidating error/finding; a later clean verdict in the same batch supersedes an earlier error, but restoring an older row cannot supersede a newer outcome. Retain a per-resource semantic fingerprint map across omitted rows so metadata-only restoration cannot become a new verdict. Different resources with equal timestamps are unordered even within one collection: an invalidation wins a conflict, and IDs only choose deterministically among equivalent outcomes. An observed semantic edit of the exact retained resource can replace it within one timestamp tick. Keep a reaction-ID high-water mark across head changes, initialized from the durable reaction cursor. Explicit cursors and catch-up preserve their requested activity streams; success coalescing defers routine monitoring, not deliberate replay.

Advance those observations on every successful poll and settle fetch. Rendering a candidate must not commit delivery progress; the existing pending transaction still owns acknowledgement. The latest head and last authenticated thread map persist independently across process boundaries; notification adoption cannot overwrite them. Bind new reactions to that observed head, not an older notification snapshot. Actual observed head transitions reset epoch evidence, but retaining a B event must not erase a fresh A verdict observed after returning to a stable A. The existing CI/report guard still prevents attaching A's CI verdict to B's event.

Fail-fast CI may tolerate missing inventory, never an older run attempt. Preserve a known terminal observation against same-attempt nonterminal regression and merge acknowledged partial failure inventories instead of replacing previously notified run IDs. Render observed unacknowledged failures even when a later raw page omits them; an emitted failure bypasses settle. These observation changes are scoped to coalescing; activity mode retains its released behavior. Preserve the existing complete-inventory requirement for a success candidate.

Malformed batching state fails only the affected waiter, with the original evidence intact and the supervisor's existing error notification. It is deferred delivery/notification history, not disposable optional-feature configuration. Clearing it can lose the only review evidence or notification watermark. This does not affect Avibe service startup; see the same distinction for Actions history in `docs/plans/watch-actions-dedup.md`.

## Circuit-breaker scope decision

The second reviewed head exposed the same observation-retention class at additional boundaries, so further blind patches were stopped. The orchestrator fetched the full GitHub inventory and obtained the independent reviewer's complete bounded inventory before authorizing one shared classifier repair: the narrow problem-text regex was not a safe negative test for either progress summaries or clean verdicts. Progress now requires the complete known summary envelope, including the exact standard footer and every expected review row, and clean verdicts require their complete two-line shape with agreeing reviewed SHAs; arbitrary trailing text, malformed rows, cross-stream ties within and across polls, and contradictory evidence remain reportable. No service, API, persistence migration, merge-policy, or external-operations scope was added. The latest-head observation and older delivered PR snapshot are intentionally separate owners; forcing their heads equal was a local workaround that erased valid new evidence.

The `832b09593` recurrence was inventoried across all nine findings-bearing reviewed heads and all 15 threads before changing the outcome producer. The remaining replay defect conflated scanning existing rows with observing a new semantic verdict: `initialize=True` bypassed retained per-resource fingerprints, so a metadata-only timestamp refresh could restore an old pass over a newer invalidation. Initialization may adopt unseen rows, but a same-head replay must use the same semantic-change rule as ordinary polling. Scope is the shared initializer and the existing replay regression matrix (comment/review, catch-up/cursor, unchanged/refreshed metadata), including genuine new-verdict recovery; no new state, classifier heuristic, prompt text, or live-Watch mutation is needed.

The `b44e519d7` inventory covers ten findings-bearing heads and all 16 threads; only the new same-stream edit-tie finding remains open. Creation IDs cannot order mutable updates, so limiting conservative ties to different collections was the wrong boundary. Apply the same timestamp/conflict rule in same-poll selection and persisted-outcome adoption, regardless of collection or resource ID. Preserve same-resource semantic replacement and fresh clean-to-clean evidence at an equal timestamp; do not make every tie permanently invalidating. The existing tie tests are consolidated into one same/cross-stream arrival-order matrix, with recovery controls, plus an equal-timestamp stronger-evidence case. No schema, new owner, regex, or prompt expansion is required.

The independent review also verified the real summary from comment `6186377372`: its inline `Reviews are triggered when you` preamble differs from the older split `Codex reviews are triggered when you` footer. Accept both complete known footer forms, keeping arbitrary footer content reportable and leaving trigger policy unchanged. The existing progress/error matrix now covers both forms; the two live-form progress cases failed before this compatibility fix, while the 12 controls passed.

## Receipt-form clean pass (2026-10-20)

Codex now posts its clean pass as a receipt comment: a `<!-- codex-review-receipt:v1 -->` marker, the verdict line
ending in a rotating sign-off (`:+1:`, "Chef's kiss.", "Keep them coming!", ...), the `Reviewed commit` line, and a
details footer that differs from the summary comment's. The classifier accepted only the bare two-line form, so on
avibe#2868 the receipt for `c02d7ae5cd` was reported as an unfamiliar payload while CI was still running; the later
green CI was then a quiet half-success, and the lane was never woken although the PR stayed Merge-ready for eight
hours. All 30 receipts on this repository's last 60 PRs have this shape.

Two changes close the class:

- The clean-pass shape accepts the complete receipt: the optional marker, a sign-off of at most 80 plain characters
  with no problem text or markup, the `Reviewed commit` line, and the exact receipt footer. Anything else, including
  text after the footer, still reports.
- Liveness for an unclassified verdict: when the latest current-head Codex verdict is neither a recognized pass nor a
  recognized problem (problem text, changes requested, inline comments, or unresolved threads), a later complete green
  CI result is reported once as ordinary CI activity, not as a success candidate. The Agent that judged the verdict
  gets the signal it is waiting for; recognized findings and errors keep green CI quiet as before.

No state field, waiter option or supervisor behavior changes.

## Validation evidence

- With only the new parser flag present, both arrival-order regressions failed because the unchanged waiter emitted a single success (`rc=0` instead of the expected quiet wait).
- The focused PR waiter, Actions waiter, repository guidance, and Harness Skill guidance suite passes after the complete observation-history repair: **386 tests**. The latest regressions cover both deliberate replay modes retaining a newer invalidating outcome, unmarked finding text in progress summaries, the live Codex state-summary footer, unknown footer content and rows, unmarked finding text appended to a SHA-bearing clean pass, cross-stream timestamp ties within and across polls, contradictory reviewed SHAs, and metadata-only restoration during both explicit replay modes. Four new replay cases failed on `832b09593` because they emitted a false success candidate; their unchanged-metadata controls passed. Four ordering cases also failed on `b44e519d7`: three same-stream arrival orders emitted false success, and an equal-timestamp SHA verdict failed to refresh reaction-only evidence. The related CLI Watch tests also passed in the earlier combined run; that run's only failure was the newly added documentation-command extractor, corrected and revalidated by the focused suite.
- Changed-file Ruff, `git diff --check`, and `askill validate skills/background-watch-hook/SKILL.md` pass.
- The normal Codex summary table shape was checked against a live read-only PR response. Its normal Running/Completed updates, including the standard About Codex details footer, are quiet; finding text outside that known shape and failure/unrecognized statuses remain visible.
- First-head independent review found ordered-outcome, reaction-epoch, settle, replay, and partial-CI-history regressions. Fourteen focused cases were run against the original `3c88ae3d1` waiter and failed for those intended behavioral reasons, including metadata-only invalidation. They now exercise the monotonic-observation contract rather than a collection of special-case retries.
- GitHub first-head findings on exact summary statuses and seeded stale passes are covered by suffix-status and initialization ordering regressions. The malformed-history suggestion is intentionally not adopted for the delivery-state reason above.
- The second-head full inventory was reproduced before the bounded repair: missing/terminal-regressed CI failures, fresh late-head reaction retention, mixed summary findings, semantic restoration with newer metadata, and three unauthenticated startup/replay variants. All repaired paths retain the existing at-least-once delivery owner and have positive recovery controls.
- Independent read-only re-review, current-head GitHub Codex review, and required CI remain delivery gates, not waived by these local results.

## Prompt surface

The routine Watch Skill entry is 100 lines, down from 602 in the initial proposal and 560 on the base branch. Detailed examples and cursor mechanics live in an optional reference that is not rendered with the entry. The PR delivery Skill replaces existing observation/report clauses rather than adding a section and is shorter than its base. This task's recurring Watch message was reduced from 727 to 160 characters without changing its command, routing, lifecycle, or cursor.

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
