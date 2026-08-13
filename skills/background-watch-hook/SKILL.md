---
name: background-watch-hook
slug: background-watch-hook
description: Use `vibe watch` to run a managed Harness waiter that returns to the same conversation later. Best for reviews, CI, files, logs, and other wait-now-continue-later workflows.
version: 0.15.0
---

# Background Watch Hook

Use this skill when the job is "wait now, continue later in the same conversation".

What it gives the agent:

- a managed background task instead of manual polling
- a clean way to come back to the same channel or thread later
- a reusable pattern that works for reviews, CI, files, logs, and process completion

Good trigger scenarios:

- PR reviews or comments may arrive later
- CI, deployments, or exports need time to finish
- a file, log line, or process exit should wake the agent up later

Prefer `vibe watch` when the wait should be inspectable, pausable, resumable, or removable later.

## Main Tools

- `vibe watch add`
  Main entrypoint. Starts a managed background watch and creates a follow-up Agent Run after the waiter succeeds or reaches a terminal failure.
- `vibe watch list`, `vibe watch show`, `vibe watch update`, `vibe watch pause`, `vibe watch resume`, `vibe watch remove`
  Use these to inspect and manage the watch after creation.
- `scripts/wait_pr.py`
  Bundled GitHub waiter for PR activity, with optional exact-head Actions monitoring.

## Use `vibe watch` First

Use `vibe watch add` first. Most tasks only need:

1. a short action-oriented message
2. a blocking waiter command

Generic shape:

```bash
vibe watch add \
  --message "<what the next Agent Run should do>" \
  --name "<optional label>" \
  -- \
  <waiter command ...>
```

Default behavior:

- returns immediately
- keeps the waiter managed by Avibe
- lets the agent inspect or stop the watch later
- creates a follow-up Agent Run after the waiter succeeds or reaches a terminal failure

Use `--forever` when the same waiter should re-arm after each detected event instead of exiting after one follow-up.

## `vibe watch` Parameters To Remember

- `--message`: the instruction template for the follow-up Agent Run created from waiter output
- `--name`: optional label for later management
- `--session-id`: only when the follow-up should continue a different explicit Agent Session
- `--create-session --same-scope`: create a visible sibling Session for the follow-up instead of continuing this conversation
- `--create-session --scope-id <scopes.id>`: create the follow-up Session in a specific existing scope
- `--forever`: re-arm after each detected event
- `--timeout`: per-cycle timeout
- `--lifetime-timeout`: whole-watch lifetime cap, mainly for forever watches

Management commands:

- `vibe watch list`
- `vibe watch show <watch-id>`
- `vibe watch update <watch-id> --name '...'`
- `vibe watch pause <watch-id>`
- `vibe watch resume <watch-id>`
- `vibe watch remove <watch-id>` hides the watch while keeping prior run history

## Waiter Contract

Write waiters to follow this contract:

- `exit 0`: event detected; final summary printed to `stdout`
- `exit 64` **plus the line `avibe-watch: no-event` on `stderr`**: cycle completed with nothing worth reporting; **no follow-up Agent Run**, the watch ends (`once`) or re-arms (`--forever`)
- `exit 124`: timeout; still send a timeout follow-up
- other non-zero: failure; the watch stops and sends a failure follow-up

Exit 64 is the token-saving path. Every other terminal exit costs one Agent turn,
so a waiter whose normal outcome is uninteresting — green CI, review chatter that
was filtered out — should end on 64 rather than reporting "nothing to do". It is a
clean ending, so a `once` watch that retires on 64 reads as completed rather than
failed, and whatever the waiter wrote to `stderr` is logged beside the watch id.

The marker is not optional. 64 is also BSD `sysexits` EX_USAGE, so a watched command
that rejects its own arguments exits with it — and it must keep failing loudly rather
than being read as a quiet cycle and, in `--forever`, rerun indefinitely. A bare 64
is therefore treated as a failure; only 64 with the marker is a no-event cycle. In
the bundled waiters, `_github_wait_common.no_event("<summary>")` prints the summary
and the marker to `stderr` and returns the code, so `return no_event(...)` is the
only place the contract has to be spelled out.

Keep the output split clean:

- `stdout`: final summary for the next turn
- `stderr`: polling logs and diagnostics

## Generic Examples

Delay:

```bash
vibe watch add \
  --name "Delay follow-up" \
  --message "The delayed check completed. Continue from the result below." \
  -- \
  bash -lc 'sleep 120; echo "Timer finished after 120 seconds."'
```

File appears:

```bash
vibe watch add \
  --name "Wait for export file" \
  --message "The export file is ready. Inspect it and continue." \
  -- \
  bash -lc 'while [ ! -f /tmp/export.json ]; do sleep 10; done; echo "Detected /tmp/export.json"'
```

Log match:

```bash
vibe watch add \
  --name "Watch app log" \
  --message "The expected log pattern appeared. Inspect the event and continue." \
  --forever \
  -- \
  bash -lc 'tail -Fn0 /tmp/app.log | while read -r line; do case "$line" in *READY*) echo "$line"; break;; esac; done'
```

## Session Targeting

Use the current Avibe context:

- Inside an Avibe-injected Agent shell, omitting the target continues this conversation.
- Use `--session-id <id>` only when the follow-up should continue a different existing Agent Session.
- Use `--create-session --same-scope` when follow-ups should run in one visible sibling Session under the same Workbench project or IM scope.
- For `--forever` watches that need a separate visible Session for each event, use `--create-session-per-run --same-scope`.
- Use `--create-session --scope-id <scopes.id>` when follow-ups should run in one Session under a specific existing scope.
- For separate visible Sessions in a specific existing scope, use `--create-session-per-run --scope-id <scopes.id>`.
- If `--cwd` is omitted while creating a Session, Avibe uses the command's current working directory.

## Timeout And Lifecycle

For `vibe watch add`:

- `--timeout` is the waiter timeout for one cycle
- default is `21600` seconds
- `0` means no per-cycle timeout
- `--forever` means re-arm after each detected event
- an allowed `--retry-exit-code` keeps either mode waiting; a once Watch stops after its first event
- `--lifetime-timeout` limits the whole long-running watch; default is `0` meaning run until killed

This separation matters: a once Watch may now have several retry cycles, and a forever
Watch can still use a bounded timeout for each cycle.

Exit `0` means one new reportable event, never merely that a condition remains true.
For a persistent level, return an allowed retry code (default `75`) until a new edge is
observed. Exit `64` plus `avibe-watch: no-event` on stderr is a completed cycle with
nothing worth reporting. A forever waiter must keep a durable cursor, state transition,
or domain cooldown so it cannot emit the same level repeatedly.

Avibe admits only one queued/running follow-up per Watch. A forever Watch re-arms after
that Agent Run settles and a five-second safety delay. If the waiter still produces six
successful events within 60 seconds, Avibe pauses the Watch and sends the target Agent
one repair message containing the bounded latest waiter output. The Agent should inspect
and fix the waiter, and resume only after verifying an unambiguous, reversible fix.

## Bundled Waiter Example

This skill ships bundled GitHub waiters:

- `scripts/wait_pr.py`
  Waits for GitHub PR review activity, including reviews, inline review comments, PR conversation comments, PR status transitions such as `draft -> open`, `open -> merged`, or `open -> closed`, and the special Codex `+1` reaction on the PR body. It can also wait for newly opened PRs in a repository.
  When `--sha` and one or more `--workflow` values are provided for a specific PR,
  the same waiter also waits for every matching Actions run at that exact SHA and
  optional branch.
- `scripts/wait_issue.py`
  Waits for GitHub issue activity, either newly opened issues in a repository or new comments on a single issue.
- `scripts/wait_action.py`
  Waits for selected GitHub Actions workflow runs on a specific commit SHA to finish
  when there is no PR activity stream to combine with them. Workflow failures are
  reported as an event so the follow-up turn can inspect and handle them.

Use bundled waiters as examples or as ready-to-run building blocks. The main skill is still `vibe watch`; the waiter is only the thing that blocks until the condition is met.
When running a bundled script through `uv`, prefer `uv run --no-project ...` so the script does not accidentally attach itself to an unrelated parent project.
Bundled GitHub waiters classify temporary network failures and GitHub
`408/429/5xx` responses as retryable. The one-shot PR and Actions waiters retry
those failures inside the same process so the managed watch stays alive. A
cycle-oriented waiter may use exit code `75` only when its supervisor explicitly
opts into retrying that code. Exit code `64` with the
`avibe-watch: no-event` marker means a cycle finished with nothing worth an Agent
turn.

The waiter is distributed with this skill, not with the caller's repository.
Resolve the active skill directory before constructing a watch command:

```bash
BACKGROUND_WATCH_HOOK_SKILL_FILE="${BACKGROUND_WATCH_HOOK_SKILL_FILE:-}"
if [ -z "$BACKGROUND_WATCH_HOOK_SKILL_FILE" ]; then
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  for SKILL_FILE in \
    "$REPO_ROOT/skills/background-watch-hook/SKILL.md" \
    "$REPO_ROOT/.agents/skills/background-watch-hook/SKILL.md"; do
    if [ -f "$SKILL_FILE" ]; then
      BACKGROUND_WATCH_HOOK_SKILL_FILE="$SKILL_FILE"
      break
    fi
  done
fi
if [ -z "$BACKGROUND_WATCH_HOOK_SKILL_FILE" ]; then
  for SKILL_ROOT in "${CODEX_HOME:-$HOME/.codex}/skills" "${AGENTS_HOME:-$HOME/.agents}/skills"; do
    [ -d "$SKILL_ROOT" ] || continue
    BACKGROUND_WATCH_HOOK_SKILL_FILE="$(find "$SKILL_ROOT" -path '*/background-watch-hook/SKILL.md' -print -quit)"
    [ -n "$BACKGROUND_WATCH_HOOK_SKILL_FILE" ] && break
  done
fi
test -f "$BACKGROUND_WATCH_HOOK_SKILL_FILE"
BACKGROUND_WATCH_HOOK_DIR="$(dirname "$BACKGROUND_WATCH_HOOK_SKILL_FILE")"
```

## GitHub Example Waiter

For a PR delivery loop, prefer one combined `wait_pr.py` watch. It observes PR review,
comment, reaction, thread, lifecycle, and head-change events together with selected
Actions workflows for one exact commit. This keeps one cursor/state file and one
follow-up Agent Run for the whole PR concern. Use `wait_pr.py` without CI arguments
for PR-only monitoring. Use `wait_action.py` only for an Actions wait that is not
attached to a PR.

### Preferred PR + CI watch

The CI arguments are one group: `--sha` and at least one repeatable `--workflow` are
required, while `--branch`, `--max-pages`, and `--success-conclusion` are optional.
The waiter stays quiet while a requested run is missing or still running, then reports
the complete exact-head result when every requested workflow has terminal runs. Every
distinct matching run ID is included, so an earlier failed rerun remains visible to
the follow-up turn. If the PR head changes, the waiter reports that event; fetch the
new head and re-arm with a new `--sha` rather than continuing to gate the old commit.

```bash
STATE_FILE="$HOME/.avibe/state/watch-cursors/pr-151-review.json"
HEAD_SHA="$(gh pr view 151 --repo avibe-bot/avibe --json headRefOid --jq .headRefOid)"
BRANCH="$(gh pr view 151 --repo avibe-bot/avibe --json headRefName --jq .headRefName)"

uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
  --repo avibe-bot/avibe --pr 151 \
  --sha "$HEAD_SHA" --branch "$BRANCH" \
  --workflow lint \
  --actionable-only \
  --state-file "$STATE_FILE" --seed-state

# Push or post the review trigger only after the combined baseline is durable.
# If the push changed the PR head after seeding, refresh both values before arming.
# This is required even when the old SHA was used to create the baseline.
HEAD_SHA="$(gh pr view 151 --repo avibe-bot/avibe --json headRefOid --jq .headRefOid)"
BRANCH="$(gh pr view 151 --repo avibe-bot/avibe --json headRefName --jq .headRefName)"

vibe watch add \
  --name "Watch PR 151 review and CI" \
  --message "PR #151 has new review activity, a head change, or exact-head CI activity. Fetch the latest PR and Actions state, resolve actionable findings, and re-arm the combined waiter for any new head. Summarise the round here in one or two lines; do not post that summary as a PR comment." \
  -- \
  uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
    --repo avibe-bot/avibe --pr 151 \
    --sha "$HEAD_SHA" --branch "$BRANCH" \
    --workflow lint \
    --actionable-only --settle 20 --state-file "$STATE_FILE" --interval 60
```

Do not combine `--new-prs` with CI arguments. A new PR has no stable exact-head
gate until the follow-up turn resolves its head and workflow set.

One-shot watch:

```bash
STATE_FILE="$HOME/.avibe/state/watch-cursors/pr-151-review.json"
uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
  --repo avibe-bot/avibe --pr 151 --state-file "$STATE_FILE" --seed-state

# Push or post the review trigger only after the baseline is durable.
vibe watch add \
  --name "Watch PR 151 reviews" \
  --message "PR #151 has new review activity. Fetch the latest review state and resolve the actionable findings on the PR. Then summarise the round here in one or two lines -- which findings you resolved and what changed -- and do not post that summary as a PR comment. Save a longer message for the review passing, the loop being blocked, or a decision that needs the user." \
  -- \
  uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
    --repo avibe-bot/avibe \
    --pr 151 \
    --actionable-only \
    --settle 20 \
    --state-file "$STATE_FILE" \
    --interval 60
```

Before a push or review trigger, seed an owner-specific state file from the
current complete PR snapshot. Arm the post-action watch with that exact file so
activity that lands during the handoff remains visible.

Use `--catch-up` only when deliberately processing historical activity. It is
not a substitute for the pre-action baseline in a review loop.

Prefer `--actionable-only` for review loops. Without it the waiter wakes the Agent
for every comment on the PR, including the `@codex review` triggers the loop itself
posts and the bodyless `COMMENTED` review envelope GitHub wraps around inline
comments. With it the waiter still reports inline review comments, reviews carrying
a verdict or a body, the Codex pass reaction, and merged/closed transitions — which
is everything the review loop needs to make progress or close out.

Narrow it further with `--ignore-author <login>` and `--ignore-comment-pattern <regex>`
(both repeatable). Filtered items still advance the cursors, so they are examined
once and never re-reported. These filters suppress review/comment payloads, not
review-thread status. A thread becoming unresolved or resolved remains an
independent wake signal because thread state is a separate mutable resource and
may later be changed by a different actor.

`--settle <seconds>` is worth setting on any review loop. A bot review arrives as a
burst of inline comments plus an envelope, so the poll that happens to catch the
first fragment would otherwise report it alone and the rest would arrive as a second
event. With `--settle` the waiter re-polls until the set stops growing (at most three
extra polls) and reports the whole batch as one event, which is one Agent turn
instead of several. 20 seconds is a reasonable starting point. The window never runs
past `--timeout`: settling is skipped unless both the wait and a full re-poll fit in
what is left of the deadline, so a batch already worth a turn is reported rather than
lost to the timeout kill. Keep `--settle` well under `--timeout`.

Catch up on existing activity first:

```bash
STATE_FILE="$HOME/.avibe/state/watch-cursors/pr-151-catch-up.json"
vibe watch add \
  --name "Catch up PR 151 reviews" \
  --message "PR #151 already has review activity. Fetch the latest review state and resolve the actionable findings on the PR. Then summarise the round here in one or two lines -- which findings you resolved and what changed -- and do not post that summary as a PR comment. Save a longer message for the review passing, the loop being blocked, or a decision that needs the user." \
  -- \
  uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
    --repo avibe-bot/avibe \
    --pr 151 \
    --state-file "$STATE_FILE" \
    --catch-up
```

Stay armed for future activity:

```bash
STATE_FILE="$HOME/.avibe/state/watch-cursors/pr-151-forever.json"
uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
  --repo avibe-bot/avibe --pr 151 --state-file "$STATE_FILE" --seed-state

vibe watch add \
  --name "Monitor PR 151 reviews" \
  --forever \
  --timeout 21600 \
  --lifetime-timeout 86400 \
  --message "PR #151 has new review activity. Fetch the latest review state and resolve the actionable findings on the PR. Then summarise the round here in one or two lines -- which findings you resolved and what changed -- and do not post that summary as a PR comment. Save a longer message for the review passing, the loop being blocked, or a decision that needs the user." \
  -- \
  uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
    --repo avibe-bot/avibe \
    --pr 151 \
    --actionable-only \
    --settle 20 \
    --state-file "$STATE_FILE" \
    --interval 60
```

Always pass `--state-file` to a `--forever` watch. Each cycle is a fresh waiter
process, so without it the next cycle re-snapshots the PR as its baseline and
anything that arrived between the previous cycle's exit and that snapshot is lost.
The file carries the resolved GitHub login and the complete mutable PR baseline
forward. PR cycles reread complete review/comment/thread collections because
edits, deletions, and thread-resolution changes are wake-worthy state.
For an ordinary wait, that normalized complete snapshot is the single wake/no-wake
decision. Numeric cursors, fingerprints, and the thread map only describe a detected
change; they cannot wake independently. `--catch-up` and explicit `--since-*-id`
flags are the deliberate replay modes and therefore remain cursor-driven. A legacy
state file with cursors but no complete snapshot is rejected until it is deliberately
caught up or reseeded, rather than silently absorbing mutable changes.
The login is reused only while the token still fingerprints to the account it was
resolved for. Explicit cursor flags still request a replay from that cursor, while
the complete PR collections remain the source of truth for edits and removals. The
state also records the last observed PR head, review/comment fingerprints, and every
review-thread resolution state, so a pushed head, edited object, deletion, or thread
transition is activity even when no new numeric ID exists. Cursors that cover a reported event are not committed by the
cycle that reports it. A waiter cannot observe its own delivery — `vibe watch` reads
its stdout only after the process exits — so those cursors are staged under `pending`
while the committed ones stay before the event, together with the rendered report
and the value of
`AVIBE_WATCH_LAST_DELIVERY` the cycle started from. That variable is when this watch
last had a report durably queued, stamped in the same transaction as the follow-up, so
any later cycle that reads a *different* value knows the report was delivered and
promotes the staged cursors. An unchanged value means it may never have been queued,
so they are dropped and the event is reported again: at-least-once, costing one
repeated Agent turn instead of losing the activity for good. Comparing a durable stamp
rather than consuming a one-shot acknowledgement is what makes this correct across a
service restart, and for a `once` watch, whose one report is followed by no cycle at
all until the user resumes it. A manual run has no supervisor, and there printing *is*
the delivery, so it commits straight after reporting. Progress made by filtering — new
activity that was deliberately not reported — commits immediately either way, since
there is no delivery to wait for. Three failures are terminal rather than a warning —
a path that cannot be written to (checked before the first poll), a path already owned
by another watch, and an existing file whose cursors cannot be read — and each stops
the watch with exit `1` rather than polling on without the cursors it was asked to
keep, or clobbering another watch's. A corrupt or unrecognised state file is left
exactly as found: re-baselining from the current PR would skip everything that
arrived after the cursor it did hold and then overwrite the only evidence of how far
the watch had got. An empty file is the one exception, since it is a claim caught
between its exclusive create and its first write and never held a cursor at all.

Ownership is claimed, not assumed. A missing state file is created before the first
poll holding nothing but the identity it belongs to, so two watches started together
cannot both see an unowned path; the one that loses that exclusive create reads the
winner's claim and stops. Ownership is re-checked before every replacement as well,
because the loser of a microsecond-wide race has already passed the startup check.
Identity is the repo, the PR, and the options that decide what the watch reports
(`--actionable-only`, `--ignore-author`, `--ignore-comment-pattern`,
`--include-self-comments`, `--new-prs`) — two watches on the same PR that report
different things cannot share cursors either, because the filtered one advances past
events the other never reported. Pacing options such as `--interval` and `--settle`
are not part of it. When `vibe watch` runs the cycle it also names the watch in
`AVIBE_WATCH_ID`, and the waiter records it as the owner, so even two watches
configured identically down to the last filter are kept apart; a manual run has no
id and adopts whatever it finds -- and a managed watch that starts on a file with no
owner, from a manual run or an older version, stamps itself on it before polling,
because an owner that is absent fits every watch and two of them would share the
path. That decision is read-then-write, so it is taken under a `<state-file>.lock`
sidecar; the lock lives beside the state file rather than on it, since the state file
is replaced rather than rewritten. Give each watch its own state file.

Arguments are validated before any of this: a rejected `--ignore-comment-pattern` or
a missing token exits `2` without claiming the path, so the corrected re-run is not
refused as a different watch's state.

GitHub-specific notes:

- `--catch-up` reports activity that already exists at startup, and overrides saved
  cursors when a state file is present; an explicit `--since-*-id` still wins
- without `--catch-up` or a `--state-file`, the waiter snapshots current PR activity as the baseline
- polling is cheap by design: mutable review/comment collections are fetched in full
  so edits and deletions remain observable, reactions are filtered server-side with
  `content=+1`, and unchanged pages revalidate to `304`, which GitHub does not charge
  against the rate limit — an idle watch can poll for hours for free
- PR activity also includes the special case where `chatgpt-codex-connector` or
  `chatgpt-codex-connector[bot]` leaves a `+1` reaction on the PR body instead of
  posting a comment; pass reactions remain visible even when `--event-limit` is
  reached
- PR activity also includes lifecycle changes on the PR itself, for example draft/ready, closed, reopened, or merged transitions
- a changed PR head is reported as activity so a new push cannot leave the review
  loop asleep
- self-authored comments are ignored by default when the current authenticated GitHub user can be resolved; pass `--include-self-comments` to keep them
- authentication is preferred; unauthenticated polling is slower and more fragile

New PRs in a repository:

```bash
STATE_FILE="$HOME/.avibe/state/watch-cursors/new-prs-avibe.json"
uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
  --repo avibe-bot/avibe --new-prs --state-file "$STATE_FILE" --seed-state

vibe watch add \
  --name "Watch new PRs" \
  --message "The repository has new pull requests. Review the new PRs and continue as needed." \
  -- \
  uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
    --repo avibe-bot/avibe \
    --new-prs \
    --state-file "$STATE_FILE" \
    --interval 60
```

New issues or issue comments:

```bash
uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_issue.py" --repo avibe-bot/avibe --new-issues --interval 60
uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_issue.py" --repo avibe-bot/avibe --issue 157 --interval 60
```

Standalone GitHub Actions for a pushed commit that is not being monitored through a PR:

```bash
vibe watch add \
  --name "Watch CI" \
  --message "GitHub Actions failed. Inspect the result below and fix the failures. Then summarise the round here in one or two lines -- what failed and what you changed. Save a longer message for the build going green, the loop being blocked, or a decision that needs the user." \
  -- \
  uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_action.py" \
    --repo cyhhao/sub2api \
    --branch main \
    --sha "$HEAD_SHA" \
    --workflow CI \
    --workflow "Security Scan" \
    --interval 60
```

For a merge gate, omit `--only-on-failure`: a successful exact-head build must
wake the Agent so it can perform the final gate and close out the watch. Use
`--only-on-failure` only when a green result intentionally needs no follow-up;
that mode exits `64` with the no-event marker and records the summary in the
Avibe watch log instead of creating an Agent turn.

## Practical Advice

- Keep messages action-oriented. Tell the next turn what to do with the waiter result.
- In a fix loop, have every round report itself in one or two lines. The user is
  watching a loop they cannot see inside: with no per-round summary all they get is a
  stream of tool activity, and no way to tell a converging PR from one that is
  thrashing on the same finding. Name the findings resolved and what changed, not a
  diff walkthrough — the detail is already in the commit and on the PR. Keep it in the
  conversation only; re-posting the same summary as a PR comment adds noise to the
  review the bot is reading. Save a longer, user-facing message for the outcomes a
  human has to act on: the review passed, the loop is blocked, or a decision has to be
  made. A reply containing only a `<silent>...</silent>` block sends nothing at all, so
  keep that for cycles that genuinely produced nothing worth reading — a re-poll that
  found no new findings — not for the rounds that did the work.
- If this is the first time using `vibe watch add`, read `vibe watch add --help` first; the help text explains both argument syntax and runtime behavior such as how `--message` and waiter stdout become the follow-up Agent Run input.
- Prefer `vibe watch` over ad-hoc detached shells when the wait should survive the current turn cleanly.
- Treat GitHub as just one example waiter, not the main point of the skill.
- If a watch is no longer useful, remove it instead of leaving stale background work behind.
