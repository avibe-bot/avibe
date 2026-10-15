---
name: pr-delivery-loop
description: Deliver Avibe, backend, docs, avault and vault-sandbox PRs through worktrees, Codex review, CI, circuit breaking and gated close-out. Required on every Agent backend.
---

# PR Delivery Loop

This file contains the operating rules. [Rationale and examples](references/rationale.md)
are optional reading for understanding a failure or the reasons behind a rule;
do not load them routinely. They introduce no additional delivery gates.

## Ownership and distribution

- The current user-started session is the **orchestrator**. A delegated lane's
  orchestrator is its dispatching session/callback target. Authority is
  **user > orchestrator > this standard**. Never delegate final review,
  orchestration, or merge approval to a newly spawned agent.
- The orchestrator decides clear, reversible, contract-preserving scope changes.
  Ask the user only for a major trade-off, irreversible risk, or genuinely
  ambiguous direction. A lane reports cross-scope gaps instead of editing peers.
- Avibe's `.agents/skills/pr-delivery-loop/` is canonical; the project-level
  entry links to it. After canonical review passes, sync the **whole maintained
  package**, including references, byte-for-byte to each repository mirror.
  Inspect companion guidance and preserve unrelated files and edits.
- `background-watch-hook` owns managed waits, waiter scripts, cursor mechanics,
  retry/settle/filter behavior, and delivery acknowledgement. Load that skill
  before a managed wait. If unavailable, report the environment blocker; do not
  vendor or reimplement it in a repository. This skill owns delivery policy.

## 1. Scope, contracts, and implementation

- Continue an assigned branch/worktree after checking its head and authorship;
  unexplained new commits may belong to a live peer. Otherwise fetch origin and
  branch from GitHub's current default branch in a separate task worktree
  (`.worktrees/avibe/<branch>` in the multi-repo workspace). Preserve dirty work.
- Stay within assigned scope. No stacked PRs: declare dependencies on unmerged
  PRs and use default-branch contracts until those dependencies land.
- Before parallel lanes fork, commit shared interface and behavior contracts to
  the base branch. Specify exact fields and their producer, consumer, signer,
  and supplier as applicable, including what each signature covers. Audit the
  complete boundary flow before dispatch
  and after integration failures; isolated peer-mocked tests are not end-to-end
  evidence. Include real data and non-ASCII boundary cases.
- Contract deviations need orchestrator approval. At final rebase, refresh any
  externally owned spec from its authoritative current source, not an old copy.
  Do not leave a required contract as an untracked draft in the primary checkout.
- Treat allocated IDs as shared contracts. Before final integration, compare
  new migration revisions/chains and other allocated namespaces against the
  latest default branch; reconcile collisions even if Git reports no conflict.
- Cancellation is asynchronous. Before rerouting a lane, confirm the original
  stopped or explicitly notify both sessions of the handover. Do not create
  competing owners by treating a cancel request as completed termination.
- State acceptance criteria as invariants. Test all existing relevant shapes,
  including unchanged state; migrations need upgrade and downgrade checks.
  When a failure class is identifiable, cover the class, not only the example.
- Run focused tests and changed-file lint, plus required repository gates
  (`npm run build` for UI work). Self-review `git diff origin/<default>...HEAD`
  for scope drift, missing contracts, secrets, and temporary artifacts.
- CI owns the repository-wide required gates; never rerun the whole unit suite
  locally as a pre-push gate. Push and let the Watch observe CI. Flows CI does
  not cover (for example E2E, integration, or manual checks) still need their
  applicable verification. For a CI-only failure, first reproduce the failing
  CI command or file, then narrow. Remove temporary test directories you create.
- Claims about another lane's code name the SHA actually read from its current
  remote branch. Findings belong to their reviewed head; reconcile them against
  the current head before making another edit.

## 2. Open the PR and establish observation

- Open a non-draft PR with a `type(scope): summary` title and explicit
  `--base <GitHub-default-branch>`. Read back the title, body, and base.
  Use `--body-file` for multiline Markdown, not shell-interpolated backticks.
- Include capability/scope, applicable scenario IDs, validation layers, residual
  manual/E2E checks, dependencies, and a **Known-by-design ledger** for intentional
  non-changes. Reply to repeated intentional findings with that ledger entry.
- Use one durable combined PR + CI Watch per owner/concern through
  `background-watch-hook`'s preferred mode: `wait_pr.py`, `--forever`, required
  `--workflow` names; no fixed `--sha` during a normal delivery loop.
  Set `--timeout 0` on both supervisor and waiter; do not impose a lifetime cap.
- Seed one complete owner-specific baseline and arm the Watch before the first
  watched push or review trigger. For a new PR, create it first, then immediately
  seed/arm before any subsequent push or explicit trigger. Never reseed, rotate,
  or replace the cursor between rounds. Recovery from an actual Watch failure
  is separate from routine delivery; retain evidence and follow the dependency.
- Keep that Watch live while pushing, replying, and resolving. During review,
  use management commands at each round's end to verify exactly one live Watch
  for this owner, PR, and concern, not merely a remembered ID. An independent
  orchestrator gate Watch is required for delegated work and has its own state;
  it is not a duplicate lane Watch. Verify the lane's observation is live too.
- Match every distinct Actions run ID for each required workflow at the current
  head and branch. Another matching run still pending or failed blocks CI, even
  if one run succeeded. Verify workflow names against actual runs. `DIRTY` plus
  absent CI calls for conflict diagnosis, not queue waiting.

## 3. Review, findings, and circuit breaking

### Bind review evidence to the current head

- After each push, confirm current-head review pickup within a few minutes;
  otherwise post `@codex review`. While awaiting review, never pause without a
  pending review or a new trigger. Do not push another head during a pending
  review. Use wait time for acceptance, integration, and final-report preparation.
- Act on auto-review findings as inline reviews. A clean auto-review may pass
  only through the PR-body reaction; trigger review when a SHA-bearing pass
  comment is needed, rather than waiting for one to appear automatically.
- Save the trigger comment ID returned by your own write and read it back.
  Check that exact comment for the Codex bot's `eyes` reaction within about two
  minutes. Never select the last comment or search bodies for the trigger text.
  The reaction disappears after completion; its later absence is not failed
  pickup. PR-body `eyes` or a current-head verdict also provides live evidence.
- Accept only Codex-authored evidence (`chatgpt-codex-connector`, often rendered
  with `[bot]`). A comment-shaped pass must say
  `Codex Review: Didn't find any major issues` and name the current reviewed SHA.
  `Reviewed commit` alone is not a pass; findings reviews use it too.
- A Codex `+1` **on the PR body** is a pass only when the original durable Watch
  captured it as new after the current head epoch began, the prior-head review
  was already terminal, and the head is unchanged. Never manufacture this
  boundary by reseeding. If the timeline cannot bind it, trigger review on the
  unchanged head and require a SHA-bearing verdict. A summary marked completed,
  another author's reaction, or an empty owner-authored review is not a pass.

### Handle the full inventory before editing

- Fetch the current head, all paginated review threads/comments and verdicts,
  and all exact-head CI runs. Count distinct findings-bearing reviewed heads;
  classify by root cause, not by comment count. Include outdated threads.
- The orchestrator independently verifies that inventory and spot-checks a
  claimed root-cause fix in the diff and a consuming test each round. A lane's
  report or green tests cannot substitute for this check.
- **Stop before another edit/push** if a class appears on two reviewed heads.
  After an architecture/data-model rewrite, three findings-bearing heads
  without a clean pass also stop the loop, even for unrelated findings.
- A lane delivers the full inventory and waits for its orchestrator's decision.
  The orchestrator diagnoses the whole class, records the scope decision, and
  continues with the smallest complete fix that is clear, reversible, and
  contract-preserving; otherwise it escalates under the ownership rules above.
  The breaker stops blind patching,
  not the work: it does not prove a rewrite necessary, and no turn ends tripped
  without a next action or a delivered report.
- Fix actionable findings, reply, then resolve each addressed thread. If another
  person's pending review prevents a reply, never delete/dismiss it: preserve
  their drafts, report the blocker, keep observation live, and continue safe
  work. Retry the reply after they submit/discard it; do not hide the open thread.
- Notify findings/errors promptly; keep half-success silent until CI/review
  both pass. Internal round notes name the PR/head, findings addressed, and
  remaining gates in one or two lines; do not add round-summary PR comments.

## 4. Complete delivery

Before closing out, verify all review gates together:

1. A valid current-head Codex pass with no real findings.
2. Unless the repository explicitly defines no CI, the full expected check set
   is present and successful, and every matching required-workflow run succeeded.
   Missing, pending, failed, cancelled, timed-out, action-required, or unreadable
   evidence blocks the gate.
3. Zero unresolved threads across the entire PR, including older/outdated heads.

Keep exact head-bound evidence in the internal gate record and lane reports.
For the user, briefly explain residual issues (or none known), what changed and
why it matters, and the next step/merge readiness. Include a clickable PR link.
SHAs, run/check/thread/Watch counts, and gate-state dumps belong in the evidence,
not the default reply; surface them only to explain a blocker or on request.
Do not turn these questions into a rigid report template.

In a user-started orchestrator session, close out in the current turn: verify
the gates, remove the Watch, then send the final report to the user. Do not
create a follow-up solely to sequence report delivery and cleanup.

Delegated run finals are callback reports with a stable PR/head or Run ID, not
scratch text. A watch-triggered lane must explicitly deliver its report or
escalation to the orchestrator using `vibe agent run --session-id <orchestrator>
--message-file <report> --no-callback` and verify the send. The orchestrator also
sends circuit-breaker decisions back explicitly; a GitHub Watch cannot observe
Session decisions. Keep a delegated lane's Watch until its report is delivered
to the orchestrator, then remove it. If delivery fails, keep observation
available for recovery.

**Do not merge without explicit user/orchestrator authorization.** Such an
instruction is final review authority, not permission to skip mechanical gates
or spawn another approver. Recheck all gates in one guarded shell conditional:
valid head-bound bot pass, zero unresolved threads, complete successful CI, and
an open non-draft PR with `mergeStateStatus == CLEAN`. Errors, empty responses,
and missing checks fail closed. Re-scan scope, confirm delegated lanes have no
running/queued work, and merge dependencies in order. Execute:

```bash
gh pr merge <validated-pr-url> --squash --match-head-commit <validated-head-sha>
```

Read back `MERGED` and the merge commit. Report a failed gate precisely. Clean
up only this merged task's clean worktree/branches, preserving unrelated work;
fast-forward the primary default checkout before any separately authorized
deployment. Neither merge nor close-out authorizes deployment or a restart.

## 5. User-facing next actions

Only the orchestrator's user-facing delivery report offers PR action buttons,
not internal lane reports or GitHub comments. Use the conversation language and
honor requested labels. When buttons are unsupported, disabled, or unknown,
use ordinary visible prose with the target PR URL, not a strip-prone footer.

| Action | Chinese | English |
| --- | --- | --- |
| Merge | 合并PR | Merge PR |
| Update local master regression | 更新回归 | Update master |
| Update and verify end to end | 更新并验证 | Update + E2E |

- When review, CI, and thread gates pass and the PR is open, non-draft, and
  `CLEAN`, offer Merge with the PR link unless the owner asked not to offer it.
  Lack of auto-merge authority does not hide this option; offering is not
  executing. A single Merge button is enough.
- Before offering a mutating action, persist the target PR and exact offered head
  in source-bound action metadata or the internal source report; the visible
  report may omit the SHA, but the callback must be able to compare that bound
  head with the freshly fetched head. If the destination cannot retain this
  binding, do not offer a label-only mutation button.
- Bind a click to its source report using actual source-message metadata, or to
  an explicit user-named target. Labels and adjacent links carry no hidden PR or
  SHA payload. If the target is ambiguous, ask for the PR URL; never guess from
  the latest mention, current worktree, or only open PR. Re-fetch the bound head
  and gates; if the offered head changed or readiness was lost, refresh the
  offer instead of using stale approval.
- After GitHub confirms **MERGED**, replace Merge with both regression choices
  only where that repository documents a local workflow. Name the local target;
  both update from master, and Update + E2E also verifies the affected flows.
  Neither runs without a separate opt-in.

### Authorized local regression updates

After the owner opts in, read [Local regression](references/local-regression.md)
before any update. It preserves the existing local target, source-verification,
and state-protection procedure; do not load it during routine PR review.
