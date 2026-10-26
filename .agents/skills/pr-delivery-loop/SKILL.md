---
name: pr-delivery-loop
description: Deliver Avibe, avibe-bot-backend, avibe-docs, avault, and vault-sandbox PRs from worktree and implementation through Codex review, CI, and Merge. Use it for implementation work on every Agent backend.
---

# PR Delivery

Your job is to deliver a change for real: pass review and CI, then Merge it, or drive it to Merge-ready so that only the user's approval remains. There is no intermediate "state" where you can stop. Whenever you are resumed, inspect the live situation first and answer two questions: what is still between this PR and Merge, and what is the most useful thing I can do now? Then do it.

The reasons behind these rules, with the failures that produced them, are in `references/rationale.md`. Read the relevant section when a rule seems wrong for your situation or a failure is unfamiliar; routine delivery does not need it.

## Keep moving

- **Fix problems.** Investigate findings, CI failures, and conflicts to their root cause; make the smallest complete fix, verify it, and push.
- **Wait for a named signal.** Waiting is real only when you can say which signal you need, who produces it, and how you will know it arrived. Let the Watch monitor it. Use the time to add tests, check consumers, and prepare the close-out report; continue as soon as the signal arrives. If you cannot say what you are waiting for, you are not waiting—you are stopped.
- **A blocker is a problem to solve, not a reason to stop.** Diagnose it yourself, break it down, try another path, and gather evidence. Stop only when a major trade-off, an authorization outside your scope, or an irreversible risk requires the user's decision; state exactly what decision is needed.
- **Trust live state only.** Each round, fetch the current head, all reviews, comments, and threads (including outdated ones), and CI for the current head. Old snapshots, previous green results, and lane conclusions are clues only.
- **Dig deeper when a pattern repeats.** If the same root cause appears on two reviewed heads, or three consecutive heads still have findings after an architectural rewrite, you are treating symptoms. Stop patching, build a complete inventory, find the real cause, define the smallest complete scope, and continue. This changes the method; it does not stop the work.

## Start and implement

- Read the repository's `AGENTS.md` first. When taking over an existing branch, verify its head and author; unexplained new commits may belong to someone still working. For a new task, create an independent worktree from the latest default branch (`.worktrees/<repo>/<branch>` in the multi-repo workspace). One branch has one writer. Do not use stacked PRs: declare unmerged dependencies in the PR body and make the code depend only on contracts already on the default branch.
- Before editing, understand who this diff affects: callers, data, migrations, and other lanes. State acceptance as invariants that cover the class of behavior, not only the immediate example; verify both upgrade and downgrade paths for migrations.
- Run focused tests, changed-file lint, and required repository checks locally (`npm run build` for UI changes). Leave the full suite to CI instead of making it a local push gate; separately verify flows CI does not cover, such as E2E or manual checks.
- Before pushing, review `git diff origin/<default>...HEAD`: scope drift, omissions, secrets, and temporary files. At the final rebase, compare allocated IDs such as migration revisions with the latest default branch; Git can miss semantic collisions.
- Open a non-draft PR with a `type(scope): summary` title and explicit `--base <default branch>`. Use `--body-file` for multiline text. State the scope, validation, remaining manual/E2E checks, dependencies, and intentional non-changes (Known-by-design), so later findings can refer to them. Read back the title, body, and base after creation.

## Keep one Watch on the PR

The `background-watch-hook` skill owns Watch mechanics: scripts, cursors, filters, retries, and delivery acknowledgements. Load it before a managed wait. This skill defines only the principle: one persistent Watch per PR and owner (the orchestrator and each delegated lane are separate owners); arm it after creating the PR and before the first watched push or review trigger; keep it alive throughout delivery; remove it at close-out. Do not rebuild it or reset its cursor between rounds: doing so can consume already-arrived reviews as old events and make a completed review look silent. If the Watch actually fails, follow that skill's recovery procedure and preserve the evidence.

## Codex review

Codex signals are easy to misread:

- Review usually starts within a few minutes after a push, shown by Codex `eyes` on the PR. If there is no sign, comment `@codex review`, record the ID of that comment, and check whether that exact comment receives Codex `eyes`. The `eyes` disappear after review; their later absence does not mean Codex never accepted the job.
- A review can also stall after pickup. When the Watch reports `Codex review stalled` (still running past its window), re-request it the same way: comment `@codex review`, record the comment ID, and verify pickup. After two stalls on the same head, tell the user instead of requesting again.
- Accept conclusions only from `chatgpt-codex-connector` (often displayed as `[bot]`). A pass must name the current head's SHA, in one of two forms: the bot's review summary comment, whose `<!-- codex-review-state:v1 … -->` line shows the current head's full `commit_sha` in `code_review` with `"status":"completed"` and `"has_any_findings":false` (the bot edits this one comment in place, so read its current body); or a comment saying `Codex Review: Didn't find any major issues` and naming the current SHA. A `+1` reaction on the PR body means some review finished with no findings, but it names no commit; treat it as a prompt to read the summary state, never as a pass on its own. If no SHA-bearing conclusion exists for the current head, trigger another review. A finding review also contains `Reviewed commit`; that alone is not a pass.
- Do not push a new head while review is running: that wastes the round and binds the conclusion to the wrong head. Use the time for acceptance checks and close-out preparation.

## Findings and review threads

A finding belongs to the head on which it was raised. Compare it with the current code before deciding whether it still applies. After fixing and verifying it, explain the change in the thread and resolve it; do not wait for CI. If you do not fix it, explain why. For an intentional design that keeps recurring, cite the Known-by-design ledger. If another person's pending review prevents a reply, do not delete or dismiss it; preserve the draft, do other safe work, and retry after it is submitted or discarded. Reply to concrete threads only; do not post round summaries on the PR.

## CI

Applicable CI is whatever this repository requires for this change. Every required workflow run for the current head must succeed or be explicitly skipped; if the same workflow still has any pending or failed run, it has not passed. Missing, queued, cancelled, timed out, awaiting approval, or unreadable results do not pass.

Unrelated third-party integrations (for example, Vercel checks from a client repository) should not block delivery: confirm they are not required checks and are unrelated to this change, mention that in the report, and continue instead of waiting for a terminal result. The same applies when they leave GitHub `UNSTABLE` rather than `CLEAN`.

When CI fails, reproduce the failing command or file locally first, then narrow the fix. If the PR is `DIRTY` and CI does not arrive, diagnose a conflict instead of continuing to wait.

## Merge-ready

The PR is Merge-ready only when all of these are true:

- The current head has a valid Codex pass and no unhandled real finding;
- All applicable CI for the current head has passed;
- The entire PR has no unresolved thread, including old and outdated heads;
- The PR is open, non-draft, conflict-free, and GitHub shows `CLEAN`.

These must all be facts about the current head. If any item is uncertain, treat it as not passed and go establish it.

## Merge

Merge follows intent, not a fixed authorization phrase:

- If the user (or the orchestrator who delegated to you) expresses the intent to merge, merge it. "It can merge now", "merge it", and "just merge" all mean merge; do not interpret them as a status request. If the user asks for a "Merge button", they want to click it themselves—offer the button.
- If nobody says so explicitly, you may merge on your own judgment only when all of these hold: the PR is Merge-ready on its exact head; the change stays within your own task; it touches no release or tag, database migration, or security boundary (authentication, credentials, Vault, sandboxing); and leaving it unmerged would block determined follow-up work. State the reason in the report. Anything else waits for the user's or orchestrator's intent.
- If the user said to wait or to confirm personally, follow the user's latest instruction.
- If uncertain, keep verifying and offer the Merge button to the user. Do not race ahead, and do not wait passively for a magic authorization phrase.

Before merging, recheck the Merge-ready conditions live; merge dependent PRs in order; and make sure no delegated lane still has running or queued work. Then:

```bash
gh pr merge <PR-URL> --squash --match-head-commit <verified-head-SHA>
```

`--match-head-commit` ensures that the exact verified head is merged. Afterward, read back `MERGED` and the merge commit, then clean up this task's Watch, worktree, and branch without touching other work.

Merge does not authorize deployment, service restarts, local regression updates, or credential changes. Each needs the user's approval.

## Talk to the user

Use the conversation's language and lead with the result. The user cares about what changed, why it matters, remaining risks, and the next step. SHAs, run counts, and thread counts are internal evidence; mention them only to explain a blocker or when asked. Do not turn the report into a gate checklist.

- **While progressing:** say what you are doing, what signal you are waiting for, what happens next, and whether you need a decision. Report findings and failures promptly; do not report half-success, and do not interrupt the user when review and CI are only partly complete.
- **When Merge-ready and waiting for approval:** say clearly that the PR can be merged, include a clickable PR link, and offer the Merge quick reply in the same response:

  ```text
  ---
  [Merge PR]
  ```

  If buttons are unavailable, say the same next step visibly and include the link.
- **When merging directly:** do not ask again or offer a stale Merge button. Merge, read back the result, and tell the user it is merged with the reason you merged directly.
- **After Merge:** when the repository has a local regression workflow (Avibe does), offer `[Update regression]` and `[Update and verify]`. Both update the local master environment; the latter also verifies the affected flows. After the user chooses one, read `references/local-regression.md` before acting.

Buttons belong only in user-facing replies, not lane reports or GitHub comments. When offering one, record the target PR and head internally. After a click, re-fetch live state for that PR; if the head changed or readiness was lost, explain it and offer again instead of using stale approval. If the target PR is unclear, ask rather than guess.

## Delegation and lanes

- The user-started session is the orchestrator and owns final judgments, merging, and user communication. A delegated lane belongs to the session that dispatched it. Authority is user > orchestrator > this skill. Do not delegate final review or merge decisions to a new agent.
- The orchestrator may make clear, reversible, contract-preserving scope changes. A lane reports work outside its scope instead of editing another lane's area. Merge shared interface contracts into the base before forking parallel lanes.
- Each round, the orchestrator fetches its own inventory and spot-checks the claimed root-cause fix and a consuming test; a lane report and green tests are not a substitute.
- When a lane finishes or needs a decision, send its report to the orchestrator with `vibe agent run --session-id <orchestrator> --message-file <report> --no-callback` and verify delivery. A Watch-triggered lane must not rely on callbacks. Send orchestrator decisions back to the lane explicitly; GitHub Watch cannot observe session decisions. Keep the lane's Watch until the report is delivered, then remove it.
- Cancellation is asynchronous. Before handing work to someone else, confirm the original lane stopped or explicitly notify both sides of the handoff; do not create two writers.
- In a user-started session, verify gates, remove the Watch, and send the final report in the same round; do not open a follow-up task only to sequence close-out.

## Maintain this skill

Avibe's `.agents/skills/pr-delivery-loop/` is the sole source of truth. After the change passes review, synchronize the entire maintained package, including `references/`, byte-for-byte to the mirrors in other repositories, preserving unrelated files in those repositories.
