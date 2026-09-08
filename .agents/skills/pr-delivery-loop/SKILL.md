---
name: pr-delivery-loop
description: Deliver implementation PRs across Avibe, avibe-backend, avibe-docs, avault, and vault-sandbox. Required for branch and scope discipline, Codex review, CI, circuit breaking, and user-facing close-out, regardless of agent backend.
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
  `background-watch-hook`: `wait_pr.py`, `--forever`, required `--workflow`
  names, optional `--branch`, and no fixed `--sha` during a normal delivery loop.
  Set `--timeout 0` on both supervisor and waiter; do not impose a lifetime cap.
- Seed one complete owner-specific baseline and arm the Watch before the first
  watched push or review trigger. For a new PR, create it first, then immediately
  seed/arm before any subsequent push or explicit trigger. Never reseed, rotate,
  or replace the cursor between rounds. Recovery from an actual Watch failure
  is separate from routine delivery; retain evidence and follow the dependency.
- Keep that Watch live while pushing, replying, and resolving. At each round's
  end, use management commands to verify exactly one live Watch for this owner,
  PR, and concern, not merely a remembered ID. An independent orchestrator gate
  Watch is required for delegated work and has its own state; it is not a
  duplicate lane Watch. Verify the lane's observation is live too.
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
  continues when the smallest complete fix is clear, reversible, and preserves
  contracts. Escalate to the user only under the ownership criteria above.
  The breaker stops blind patching; it neither proves a design rewrite necessary
  nor transfers the decision automatically to the user.
- Fix actionable findings, reply, then resolve each addressed thread. If another
  person's pending review prevents a reply, never delete/dismiss it: preserve
  their drafts, report the blocker, keep observation live, and continue safe
  work. Retry the reply after they submit/discard it; do not hide the open thread.
- Internal round reports identify PR/head, findings addressed, and remaining
  gates in one or two lines. Do not add redundant round-summary PR comments.

## 4. Deliver, then close out

All close-out gates must hold together:

1. A valid current-head Codex pass with no real findings.
2. Unless the repository explicitly defines no CI, the full expected check set
   is present and successful, and every matching required-workflow run succeeded.
   Missing, pending, failed, cancelled, timed-out, action-required, or unreadable
   evidence blocks the gate.
3. Zero unresolved threads across the entire PR, including older/outdated heads.
4. A delivered final report naming repository, PR URL, reviewed head, changes,
   validation layers, and residual manual/E2E work.

Delegated run finals are callback reports with a stable PR/head or Run ID, not
scratch text. A watch-triggered lane must explicitly deliver its report or
escalation to the orchestrator using `vibe agent run --session-id <orchestrator>
--message-file <report> --no-callback` and verify the send. The orchestrator also
sends circuit-breaker decisions back explicitly; a GitHub Watch cannot observe
Session decisions. In a user-started orchestrator session, deliver to the user.

Keep the Watch until the report is delivered, then remove it. This order also
applies when the gates were already green when the turn started. If delivery
fails, observation remains available for recovery.

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

Only the orchestrator's final **user-facing** report offers action buttons,
never an internal lane report or GitHub comment. Name one repository/PR and its
reviewed head. Use actual destination capability and reply-enhancement settings:
when supported and enabled, append the trailing `---` and `[label]` syntax from
Avibe's `core/prompts/quick-replies.md`, outside code fences. Otherwise, including
unknown capability/settings, use ordinary visible prose with the PR URL in each
suggested reply, without a trailing separator/bracket row that could be stripped.

Use the conversation language, English when unknown, and honor explicit labels:

| Action | Chinese | English |
| --- | --- | --- |
| Merge | 合并PR | Merge PR |
| Update local master regression | 更新master到回归环境 | Update master |
| Update and verify end to end | 更新回归环境并进行端到端验证 | Update + E2E |

**Bind every action to its target before mutation.** The callback submits only
the label; adjacent prose and Markdown links add no hidden payload. Recover the
source report from actual source-message metadata when available. If unavailable
or not uniquely bound to a repository/PR, ask for the PR URL. Never select the
latest mention, current worktree, or only open PR. An explicit user target
resolves ambiguity without extra confirmation; do not change requested labels
or invent a payload format.

- Offer **Merge** only when the review, CI, and thread gates pass and the PR is
  open, non-draft, and `CLEAN`. Offering it is not authorization. After binding a
  click, re-fetch the head and gates; if the offered head changed or readiness
  was lost, report that change and refresh the offer instead of using stale approval.
- After GitHub confirms the named PR is **MERGED**, replace Merge with both
  regression choices and name the local target. Closed/unmerged is not merged.
  Update + E2E performs the same update, then relevant end-to-end verification.
  Both are separate opt-ins, not implied by merge. For Chinese reports:

  ```text
  ---
  [更新master到回归环境] | [更新回归环境并进行端到端验证]
  ```

### Authorized local regression updates

For Avibe, bind the action and recheck `MERGED`, then follow
`docs/regression/README.md`. Other repos offer these choices only when their own
documented local workflow applies; never invent a target.

1. Fetch origin and fast-forward the **primary** checkout with `--ff-only`;
   verify `master` equals the fetched `origin/master` SHA. Establish that the
   actual `sync_source()` input, including ignored deployable files, matches that
   Git tree under the runner's sender exclusions, and keep it unchanged through
   the update. Git status or `dirty=false` alone is not proof. A mismatch or an
   unprovable match blocks mutation; never stash, discard, or commit user edits
   to bypass it. Invoke the runner from this verified checkout, not a task tree.
2. Confirm the persistent **local** master environment (`avr-master` /
   `avibe-master` by default), product config, and runtime environment file exist
   and are readable. Missing state is a blocker, not provisioning/re-seeding
   authority. Use the documented local Lima route on macOS; preserve existing
   bind/port settings explicitly when different from defaults.
3. For the existing target, run from the verified checkout:

   ```bash
   python3 scripts/incus_regression.py up --target master --reset-mode none --clean --env-file /dev/null
   ```

   `--env-file /dev/null` suppresses automatic env discovery and leaves
   `/etc/avibe-regression.env` untouched; an empty regular file does not.
   Stop before mutation if the checked-out runner cannot preserve this behavior.
   `--clean` reconciles disposable source/build output, including same-size,
   same-mtime stale bytes. Preserve credentials, runtime environment, product
   state, pairing, agent homes, and sessions; neither choice authorizes a reset.
4. Let the runner rebuild assets. Verify the receipt and served source commit
   match the intended SHA and the runtime environment is unchanged without
   exposing values, before success or E2E checks. Report environment, deployed
   commit, and actual test results; health alone is not an end-to-end pass.

Neither choice authorizes production, a remote tenant, or the running local Avibe
to be updated or restarted.
