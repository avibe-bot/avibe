---
name: pr-delivery-loop
description: The implementation-lane standard for delivering a PR across the Avibe repositories (avibe, avibe-backend, avibe-docs, avault, and vault-sandbox) — branch/scope rules, contracts, Codex-bot review-loop discipline, and close-out criteria. Use this skill for every implementation task in these repos, regardless of agent backend.
---

# PR Delivery Loop — implementation-lane standard

## Distribution

- The canonical maintained copy lives in the Avibe repository at
  `.agents/skills/pr-delivery-loop/SKILL.md`.
- Every other applicable repository carries a byte-for-byte copy of this skill
  file at the same repo-relative path. Do not copy watcher implementations or
  their tests into this skill.
- In the multi-repo workspace, the project-level skill entry is a symlink to
  the Avibe copy. Update the canonical copy first, then sync every repository
  copy only after the canonical Avibe change passes review.

## Dependency boundary

- Use the `background-watch-hook` skill for every managed wait. It owns the
  reusable `vibe watch` workflow and the GitHub PR, issue, and Actions waiter
  implementations.
- This skill owns Avibe-specific delivery policy: branch and scope rules,
  review and CI gates, thread resolution, circuit breaking, authority, and
  close-out criteria.
- Refer to the dependency by skill name. Do not hard-code an installation path,
  vendor its scripts here, or hand-roll a replacement waiter. It is a
  workspace-level dependency, not a repository payload: companion repositories
  must not copy it just to satisfy this policy. The active agent environment
  must expose it by skill name before a managed wait starts; if it does not,
  report that environment blocker rather than changing repository scope.

## Roles & authority

- Your **orchestrator** is the agent session responsible for integration and
  scope decisions. For a delegated lane, it is the dispatching session (your
  run's callback target). In a user-started working session, the current agent
  is the orchestrator; the user remains the owner and highest authority, not
  the default decision engine.
- Authority order: **user > orchestrator > this standard.** An explicit
  instruction from the user or your orchestrator overrides any rule below —
  verify the mechanical gates, then carry it out yourself.
- Never spawn another agent to act as orchestrator, final reviewer, or
  merge-approver. Review authority cannot be conjured by a delegated lane:
  report decisions to its assigned orchestrator. When the current session is
  the orchestrator, decide independently; ask the owner only for a major
  trade-off, irreversible risk, or genuinely ambiguous direction.

## 0. Scope & branch

- If the owner or orchestrator gives you an existing branch or worktree, treat
  it as the assigned task context and continue there after verifying its head;
  do not create a fresh default-branch lane that abandons or duplicates it.
- Otherwise branch from the **latest** origin default branch (`git fetch
  origin` first). In avibe, use a task worktree under the workspace's
  `.worktrees/avibe/<branch>` directory, or the equivalent sibling worktree
  directory in a standalone clone.
- Stay inside your assigned file scope. A cross-lane interface gap is a
  **report to the orchestrator**, never an edit to the other lane's files.
- No stacked PRs. If you depend on an unmerged PR, build against its documented
  shapes from the default branch and declare the dependency in your PR body.
- **A unique identifier you allocate is a cross-lane contract, even though git
  treats it as a private file.** Two lanes each add
  `storage/alembic/versions/<date>_00NN_*.py` picking the same next number; the
  filenames differ, so git merges both silently into a duplicate revision and a
  forked head, and the one textual conflict — the shared `HEAD_REVISION` line in
  `tests/test_sqlite_state_migration.py` — resolves mechanically ("same string,
  keep it") and erases the evidence. Before final rebase, re-diff your
  `revision` / `down_revision` against the default branch's newest revision;
  renumber and re-chain rather than keeping siblings.
  Same shape for any allocated-id namespace: route paths, feature-flag keys,
  fixture ids. `git merge-tree --write-tree` will not find it.
- **`DIRTY` + zero CI checks is one symptom, not two.** A conflicted PR cannot
  produce the `refs/pull/N/merge` ref that `pull_request` workflows run
  against, so no workflow starts at all. Before waiting on a slow queue, check
  `gh pr view --json mergeStateStatus`.
- **A cancel-and-reroute is not a handover.** `vibe runs cancel` is
  asynchronous and can lose the race, so a brief rerouted to a fresh agent can
  leave two agents live on one branch. The run table shows nothing wrong; the
  symptom is commits the current agent did not author and a PR head ahead of its
  own record. Whoever reroutes owes one of two things: confirmation that the
  original is dead before the fork starts, or an explicit statement to each side
  — the fork is told which session it replaces, the original is told it has been
  superseded. Whoever inherits a branch reads `git log` for authors other than
  itself before editing, and treats an unexplained commit as a live peer rather
  than as its own forgotten work.

## 1. Contracts before code (multi-lane work)

- Interface shapes live in files both lanes can read (types, example payloads,
  endpoint lists, or a spec section explicitly named as the contract) — never
  only in prose. Field names are exact (case included). **Commit them to the
  base branch before the lanes fork**: a contract that lands after the fork is
  not a shared reference, it is two divergent copies.
- **An untracked contract draft in the main checkout is a defect with a delayed
  cost.** No lane can read it from `origin/<base>`, and it blocks the
  post-merge fast-forward of that checkout — which matters because regression
  deploys the local commit, so the checkout that cannot fast-forward is exactly
  the one that ships stale code. Commit the contract, or keep it out of the
  checkout entirely.
- **Freeze behavior contracts, not just field shapes.** For every
  cross-boundary flow, the contract states per field: who produces it, who
  consumes it, what a signature covers, and — for any handshake/attestation —
  which side must supply what. A shape-only contract passes both sides' unit
  tests while the behavior silently disagrees.
- Orchestrator duty: before dispatching multi-lane work, walk each end-to-end
  flow across all boundaries and enumerate every field's
  produce/consume/sign/supply, surfacing every misalignment at once. On any
  live integration failure, audit the whole remaining chain in one pass — each
  fix can mask the next layer.
- Require a real end-to-end test that pierces every boundary with real data
  (including non-ASCII) — two isolated unit suites with the peer mocked prove
  nothing about the boundary.
- Deviating from the contract requires orchestrator sign-off first.
- Orchestrator-owned spec/contract files you were given by absolute path live
  OUTSIDE your branch (often uncommitted). If your PR needs them in-repo, sync
  the AUTHORITATIVE current content at final rebase (confirm the path with the
  orchestrator) — never commit the possibly-stale copy you read at kickoff:
  amendments accumulate while you work.

## 1a. State criteria and tests as invariants, never as enumerations

- Every acceptance criterion and every test states the property that must hold,
  never the list of cases that must not happen. A list reads as complete and
  never is: the case nobody thought of is, by construction, absent from it.
- The mechanical form: seed one row of every shape that already exists, run the
  change, and assert the rows are unchanged — never list which shapes are
  skipped. A test naming the skipped ones passes forever while the one shape it
  never named is silently rewritten; seeding is complete by construction, so a
  shape added later is covered without editing the test. For a migration, assert
  after upgrade **and** after downgrade.
- Same rule for spec criteria: name the property and let the tests enumerate. An
  enumeration written into a spec becomes the definition of done, so whatever is
  missing from it falls out of scope by accident rather than by decision.

## 2. Pre-PR checks

- Run the smallest relevant local validation: focused tests for what you
  touched, lint on changed files, the build gate (`npm run build` for UI work),
  and any repository-specific required checks. Self-review your own diff once
  (`git diff origin/<default>...HEAD`) for scope strays and leftovers.
- The GitHub Codex bot review (§4) is the review gate.

## 3. Opening the PR

- Non-draft (drafts don't trigger review). Title `type(scope): summary`.
- Pass the repository's default branch explicitly with `gh pr create
  --base <default-branch>`; derive it from GitHub rather than trusting a local
  `branch.<name>.gh-merge-base` setting. After creation, read the PR back and
  verify `baseRefName` is that default branch before treating the PR as open.
- Body must include: the changed capability; affected scenario IDs when a
  catalog exists; evidence layers (unit / contract / scenario / residual manual
  checks); explicit dependencies ("requires #NNN merged first"); and a
  **Known-by-design ledger** (§4) when applicable.

## 3a. Turn-final text = a delivered status line

When you run as an async lane (`vibe agent run`), the FINAL TEXT of every turn
you end is delivered to your orchestrator's conversation as an at-least-once
callback. It is a report surface, not scratch space: include a stable PR/head
or Run identifier so duplicate callback delivery can be deduplicated, and end
every turn with a short, meaningful status line — e.g. `PR #921 head 41c088c5:
review triggered (👀 confirmed); watch armed` — never a step narration ("Push
branch, confirm repo"), a thinking fragment, or a bare next-action note. If a
turn ends because you armed a watch and are waiting, say exactly that.

## 4. Review-loop discipline

- The Codex bot usually auto-reviews new pushes, but not reliably. After every
  push, confirm a review of the new head is in flight within a few minutes; if
  none appears, comment `@codex review`. An auto-review that finds something
  submits a review with inline threads, exactly like a triggered one — act on
  it. Only the passing case is asymmetric: it announces through the PR-body
  reaction alone, never the sha-bearing pass comment, which is produced by an
  explicit trigger and by nothing else. Triggering is therefore the only route
  to comment-shaped head-bound pass evidence.
- A trigger only counts once the bot reacts 👀 (`eyes`) to that comment. The
  trigger is the comment ID your own `gh pr comment <pr> --body '@codex review'`
  call returned — identify it by that ID. Never use `issues/<pr>/comments --jq
  '.[-1]'`, and never find one by matching `@codex review` in comment bodies:
  the bot quotes that phrase in the boilerplate appended to its own verdicts, so
  the pattern selects its output as readily as your input. Query
  `repos/<o>/<r>/issues/comments/<comment-id>/reactions` within ~2 minutes and
  require `content == "eyes"` from the Codex bot (`chatgpt-codex-connector` in
  the API, often displayed as `chatgpt-codex-connector[bot]`); aggregate counts
  or other users' reactions do not prove pickup. **The bot withdraws the
  reaction when the review completes**, so 👀 is evidence only inside that
  window — afterwards every trigger reads 0, including the reviewed ones. Never
  infer "it never started" from a reaction count after the fact; look for a
  current-head verdict instead.
- Liveness invariant: at every pause there is either a pending bot review of
  the current head, or one you just triggered. Never wait on nothing.
- Use `background-watch-hook` to create one durable `--forever` combined PR watch
  for the whole delivery loop. Prefer the bundled `wait_pr.py` with each required
  `--workflow` and optional `--branch`, but omit `--sha`: every cycle resolves the
  PR's current head and observes Actions at that exact SHA. A push is a head-change
  event, not a reason to replace the Watch. Add `--sha` only for an intentionally
  fixed-head one-shot wait. Use `wait_pr.py` without CI arguments for PR-only
  monitoring, and reserve `wait_action.py` for Actions targets that are not attached
  to a PR.
- Set the durable PR Watch's per-cycle `--timeout 0`. The Harness default is
  21600 seconds, so leaving it implicit turns six quiet hours into a terminal
  timeout; an idle PR is expected state, not a failed cycle.
- The CI waiter matches every distinct Actions run ID for each requested
  workflow name at the exact SHA and branch. A workflow name is not a unique
  run identity; do not declare CI complete while a second matching run is
  pending or failed.
- The one-watch invariant is scoped by owner and concern: one live lane/fix
  watch per PR, plus one independent orchestrator gate watch when work is
  delegated. Review activity and the PR's exact-head CI are one lane concern
  and should use the combined `wait_pr.py` watch rather than two sibling
  waiters. Each genuinely independent concern needs its own state; never share
  cursor state between concurrent watches or count unrelated global monitors as
  the lane watch.
- Follow `background-watch-hook` for waiter commands, state, baseline seeding,
  catch-up, filtering, settling, retries, and delivery acknowledgement. Those
  mechanics belong to the reusable skill; this policy only constrains when and
  why the watch is armed.
- Before the first push or review trigger, use `background-watch-hook` to seed one
  complete owner-specific state file, then arm the forever Watch with that same
  file. This is the only routine baseline creation in the loop. Never reseed,
  rotate, or replace it between rounds: review or CI activity can land while the
  current follow-up runs, and making the already-arrived event a fresh baseline
  silently drops it. Catch-up is for deliberately replaying historical activity
  before the loop starts, and a replacement Watch is recovery from a real
  failure — neither is normal per-round operation.
- Keep the Watch alive while pushing, replying, and resolving threads. Those own
  actions may produce one extra batched callback, but they cannot consume the Watch
  or leave the real next review unobserved. End every round by using the
  `background-watch-hook` management commands to verify exactly one live Watch for
  this owner, concern, repository, and PR. Do not rely on a remembered Watch ID or
  one bookkeeping field as proof that its waiter is live.
- For whoever gates the PR: a lane run that ended `succeeded` proves nothing
  about the loop. A watch-triggered run can finish clean having pushed nothing
  and armed nothing, leaving the PR with new findings and no watcher on either
  side. When your gate watch fires on a findings review, verify the lane still
  has a live PR watch before concluding it has the round handled.
- The bot has three verdict shapes. Findings arrive as a review with inline
  threads. A PASS is either (a) a plain issue comment by the Codex bot whose
  body says "Codex Review: Didn't find any major issues" and names
  `Reviewed commit: <sha>` equal to the current head, or (b) a `+1` reaction
  from that bot on the PR body. Nothing else is one: not a contributor comment
  quoting the pass text, not a reaction by another author, not a reaction on any
  other comment.
- That PR-body reaction is one state slot, not an append-only log: 👀 while a
  review runs, `+1` once one completes with no comments, each write withdrawing
  the last. So 👀 there means a review is running right now — a more durable
  liveness probe than the 👀 on your trigger comment. The `+1` names no sha, but
  its `created_at` is not stale either: it is when the most recent completed
  review passed, and the only open question is which head that review ran
  against. So do not push another head while a review is pending, and accept the
  reaction when the durable Watch reports it as new after the current head epoch
  began, the prior-head review was already terminal, and the PR head is
  unchanged. Never reseed to manufacture that boundary. When the timeline cannot
  settle it — an intervening head that was never reviewed, say — force the
  binding rather than infer it: comment `@codex review` on the unchanged head
  and take whatever verdict returns, a pass comment naming the sha or a findings
  review naming it. Waiting for that comment instead of triggering waits
  forever. The slot's 👀 → `+1` transition cannot stand in for it either: the
  mandated waiter queries PR-body reactions with `?content=+1`, so it never sees
  the 👀 half.
- Do not treat `Reviewed commit:` alone as a pass signal. A findings verdict is
  a `COMMENTED` review whose body also opens
  `### 💡 Codex Review … **Reviewed commit:** <sha>`, so a gate that merely
  greps for the sha merges over live findings. Match the bot author, pass
  phrase, and exact head together for a comment-shaped pass; require the
  head-bound waiter evidence above for a reaction-shaped one. Neither is enough
  alone: the bot double-passes commits, so close-out also gates on zero
  unresolved threads across the entire PR, including threads opened on earlier
  or outdated heads — never on a quiet latest review.
- A review attributed to the repo owner with an **empty body** is a phantom, not
  a review: replying to a review thread creates a `COMMENTED` review under your
  own account, stamped with the current head's `commit_id`. Any check that
  selects reviews by head sha will count it as "the bot has started". Filter it
  out by author and empty body.
- Resolve every thread you address (reply, then resolve). For intentional
  non-changes the bot keeps re-flagging: keep a **Known-by-design ledger** in
  the PR body and answer re-flags by linking the entry.
- If GitHub refuses your thread reply because another user (often the repo
  owner) has a **PENDING review** on the PR, never delete or dismiss that
  pending review to unblock yourself — it may hold unsubmitted draft comments,
  and deletion destroys them unverifiably. Stop, report the block to the
  orchestrator/owner, and continue the round without the reply (push, re-arm
  the watch, note the unreplied threads in your report); reply only after the
  owner submits or discards the draft themselves.
- Same-theme findings = stop patching. The trigger is not a round count, it is
  whether you can **name the class**: the moment you can enumerate the members
  the reviewer has not reached yet ("it flagged the projected definition name,
  and the projection also carries session title, session label, and the
  callback session"), close the class now — one shared declaration plus a test
  that asserts the enumeration, so a member added later fails a test instead of
  costing a review round. Waiting for a third round is paying for a review you
  have already predicted. Can't name the class? A delegated lane escalates the
  inventory to its orchestrator; the orchestrator diagnoses whether the closure
  belongs in a local path, chokepoint, contract, or data model.
- The circuit breaker is mechanical, not discretionary. Before editing any
  findings review, fetch every paginated thread and record the reviewed head,
  number of findings, and root-cause classes in the status delivered to the
  orchestrator. If a class appears on a second reviewed head, stop before the
  next edit or push. After an architecture or data-model rewrite, the third
  findings-bearing head also stops the lane even when no class has repeated.
  A delegated lane delivers the complete inventory and waits for its
  orchestrator. The orchestrator diagnoses the full inventory, records the
  scope decision, and continues independently when the smallest complete action
  is clear, reversible, and contract-preserving; it asks the owner only for a
  major trade-off, irreversible risk, or genuinely ambiguous direction. Local
  tests, CI, and resolved old threads cannot waive the diagnosis.
- Searchable-list invariant, learned the expensive way on #1023 (three rounds):
  **every string a row displays must remain a substring of a column the search
  predicate covers.** A projected field the predicate forgot breaks it; so does
  a display-side transform that produces a string no column contains
  (whitespace collapsing, ellipsizing, reformatting). Deleting the transform is
  not always available: HTML collapses runs of whitespace when it renders, so a
  row can differ from its column even with no JS touching it. When the rendering
  layer itself normalizes, make the **matcher** tolerant at the single
  chokepoint that builds the pattern — never mirror the transform field by
  field, which puts every future display tweak on a treadmill. Assert the
  invariant over the enumerated title/label cases, not one example.
- **Every claim about code you did not write names the sha it was read at.**
  Read another lane's file from `origin/<their-branch>`, never at your own
  merge-base: a merge-base snapshot ages the moment they push, and reading one
  is how a lane escalates a defect the sibling fixed minutes earlier. Name the
  sha in the escalation so the reader can tell a live read from a stale one.
- A review finding — and any ruling derived from it — is pinned to the head the
  review ran on, so name that head when you act on it. If the branch has moved
  since, reconcile the finding against the current head rather than re-doing the
  fix; the work may already be there.
- **Delivery rule.** An escalation — and the §5 final report — only counts once
  it is delivered to the orchestrator: a watch-triggered run must send it to the
  orchestrator's session with `vibe agent run --session-id <orchestrator>
  --no-callback`, then verify the send succeeded. Merely finishing the run
  leaves the result in the lane session and notifies nobody. `--no-callback`
  matters: without it the orchestrator's next user-facing reply is auto-queued
  back into YOUR session as a stray instruction. While blocked, the PR waiter
  observes GitHub activity only, never Session decisions, so the orchestrator
  must deliver the circuit-breaker decision explicitly to the lane session (for
  example `vibe agent run --session-id <lane-session> --message-file <decision>
  --no-callback`). Keep the GitHub watch armed and state exactly which decision
  the lane needs.

## 5. Close-out — all conditions, then stop

1. Bot review of the current head with no real findings;
2. CI fully green: the repository's expected check set is present (unless the
   repository explicitly defines no CI), every applicable check is terminal,
   and none is failing, cancelled, timed out, action-required, or pending;
3. Zero unresolved review threads across the entire PR, regardless of the head
   on which each thread was opened;
4. Post the final report: PR URL, what shipped, evidence layers, residual
   manual checks (state what end-to-end verification is deferred to the
   orchestrator's integration pass). Deliver it by §4's delivery rule, or
   finish the run the orchestrator dispatched with it as the result; ending a
   watch-triggered round without that send means the orchestrator never learns
   you finished.
   **Tripwire:** the round where the clean pass finally lands is exactly the
   round where lanes forget this and stop after tidying watches. When you notice
   conditions 1–3 are already true at the start of a watch-triggered round, SEND
   THE FINAL REPORT FIRST, then do cleanup;
5. Remove your watches only after report delivery is verified (nothing
   dangles, and a failed delivery still has recovery liveness);
6. **Do not merge on your own initiative** — hand back; the orchestrator does
   the final review and merge. If the user or your orchestrator explicitly
   tells you to merge, that instruction IS the final review: check the
   mechanical gate yourself in one guarded shell conditional that evaluates
   every condition together — either a bot-authored pass-phrase issue comment
   naming the current head or a head-bound Codex `+1` captured by the current
   phase's waiter, zero unresolved threads across the entire PR, the expected
   CI check set is present and fully successful, and
   `gh pr view --json mergeStateStatus` == `CLEAN` — so that a check which
   errors, returns empty, or omits an expected check reads as *do not merge*.
   Then merge the validated PR explicitly with
   `gh pr merge <validated-pr-number-or-url> --squash
   --match-head-commit <validated-head-sha>` — no re-review, no spawning
   anyone. The explicit PR target and `--squash` keep the command
   noninteractive even when the orchestrator is running outside the PR's
   worktree. If the gate is not CLEAN, report exactly what's missing instead
   of refusing by role.

## 6. While waiting, don't idle

Bot rounds take 5–20 minutes. Use the gaps to prepare integration assets: the
user-facing acceptance checklist, regression/deploy prep notes, contract notes
for dependent lanes, and your final-report draft.

## 7. Orchestrator counterpart (for dispatchers, not lanes)

- Do not rely on lane terminal reports alone: arm your OWN durable `--forever`
  gate Watch per PR through `background-watch-hook`, with independent state, the
  follow-up in your session, and merge-gate instructions in the message. Lanes go
  silent at exactly the moment that matters (§5.5 failure mode); your gate Watch is
  the insurance.
- One waiter per concern still holds: the lane's watch drives its fix loop;
  your watch drives the merge gate. Two watches on one PR, two concerns — fine.
- Every time a findings review lands, independently paginate the threads and
  update the head/class counts before allowing another fix push. On each round,
  inspect at least one claimed root-cause fix in the diff and one consuming test;
  do not accept "all tests green" or the lane's classification as a substitute.
  Enforce the circuit breaker above: a repeated class on the second reviewed
  head, or a third findings-bearing head after a model rewrite, pauses the lane
  and requires the orchestrator to make and record a whole-model decision before
  work resumes.
- At gate: verify pass-on-current-head + all expected CI green + zero unresolved
  across the entire PR + CLEAN yourself from GitHub, re-scan the final diff
  against the granted file scope (plus any ratified extensions), confirm the
  lane session is quiesced (no running or queued runs), then merge in dependency
  order and ff your local default branch before any deploy.
