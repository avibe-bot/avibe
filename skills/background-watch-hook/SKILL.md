---
name: background-watch-hook
slug: background-watch-hook
description: Use `vibe watch` to run a managed Harness waiter that returns to the same conversation later. Best for reviews, CI, files, logs, and other wait-now-continue-later workflows.
version: 0.11.6
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
  Bundled waiter example for one common case: GitHub PR review activity.

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
- forever retries only when the waiter exits with an allowed `--retry-exit-code`; other failures stop the watch and send a failure follow-up
- `--lifetime-timeout` limits the whole long-running watch; default is `0` meaning run until killed

This separation matters: a forever watch can still use a bounded timeout for each cycle.

## Bundled Waiter Example

This skill ships bundled GitHub waiters:

- `scripts/wait_pr.py`
  Waits for GitHub PR review activity, including reviews, inline review comments, PR conversation comments, PR status transitions such as `draft -> open`, `open -> merged`, or `open -> closed`, and the special Codex `+1` reaction on the PR body. It can also wait for newly opened PRs in a repository.
- `scripts/wait_issue.py`
  Waits for GitHub issue activity, either newly opened issues in a repository or new comments on a single issue.
- `scripts/wait_action.py`
  Waits for selected GitHub Actions workflow runs on a specific commit SHA to finish. Workflow failures are reported as an event so the follow-up turn can inspect and handle them.

Use bundled waiters as examples or as ready-to-run building blocks. The main skill is still `vibe watch`; the waiter is only the thing that blocks until the condition is met.
When running a bundled script through `uv`, prefer `uv run --no-project ...` so the script does not accidentally attach itself to an unrelated parent project.
Bundled GitHub waiters use exit code `75` for retryable startup errors such as temporary network failures or GitHub `408/429/5xx` responses, and exit code `64` with the `avibe-watch: no-event` marker when a cycle finished with nothing worth an Agent turn.

## GitHub Example Waiter

Use the bundled GitHub waiter only when the watched thing is PR review activity.

One-shot watch:

```bash
vibe watch add \
  --name "Watch PR 151 reviews" \
  --message "PR #151 has new review activity. Fetch the latest review state and resolve the actionable findings on the PR. Do not describe the fixes in this conversation: end the turn with a silent block unless the review passed, you are blocked, or a decision needs the user." \
  -- \
  uv run --no-project scripts/wait_pr.py \
    --repo avibe-bot/avibe \
    --pr 151 \
    --actionable-only \
    --settle 20 \
    --interval 60
```

Prefer `--actionable-only` for review loops. Without it the waiter wakes the Agent
for every comment on the PR, including the `@codex review` triggers the loop itself
posts and the bodyless `COMMENTED` review envelope GitHub wraps around inline
comments. With it the waiter still reports inline review comments, reviews carrying
a verdict or a body, the Codex pass reaction, and merged/closed transitions — which
is everything the review loop needs to make progress or close out.

Narrow it further with `--ignore-author <login>` and `--ignore-comment-pattern <regex>`
(both repeatable). Filtered items still advance the cursors, so they are examined
once and never re-reported.

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
vibe watch add \
  --name "Catch up PR 151 reviews" \
  --message "PR #151 already has review activity. Fetch the latest review state and resolve the actionable findings on the PR. Do not describe the fixes in this conversation: end the turn with a silent block unless the review passed, you are blocked, or a decision needs the user." \
  -- \
  uv run --no-project scripts/wait_pr.py \
    --repo avibe-bot/avibe \
    --pr 151 \
    --catch-up
```

Stay armed for future activity:

```bash
vibe watch add \
  --name "Monitor PR 151 reviews" \
  --forever \
  --timeout 21600 \
  --lifetime-timeout 86400 \
  --message "PR #151 has new review activity. Fetch the latest review state and resolve the actionable findings on the PR. Do not describe the fixes in this conversation: end the turn with a silent block unless the review passed, you are blocked, or a decision needs the user." \
  -- \
  uv run --no-project scripts/wait_pr.py \
    --repo avibe-bot/avibe \
    --pr 151 \
    --actionable-only \
    --settle 20 \
    --state-file ~/.avibe/state/watch-cursors/pr-151.json \
    --interval 60
```

Always pass `--state-file` to a `--forever` watch. Each cycle is a fresh waiter
process, so without it the next cycle re-snapshots the PR as its baseline and
anything that arrived between the previous cycle's exit and that snapshot is lost.
The file also carries the `since` filters and the resolved GitHub login forward, so
a resumed cycle asks GitHub only for what is new instead of re-reading the whole PR.
The login is reused only while the token still fingerprints to the account it was
resolved for, and an explicit `--since-review-comment-id` /
`--since-issue-comment-id` drops the matching saved `since` so the replay it asks
for is not narrowed away. It is written on every cursor advance and on timeout,
replaced atomically. Cursors that cover a reported event are not committed by the
cycle that reports it. A waiter cannot observe its own delivery — `vibe watch` reads
its stdout only after the process exits — so those cursors are staged under `pending`
while the committed ones stay before the event, together with the value of
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
- polling is cheap by design: comment fetches are filtered server-side with `since`,
  reactions with `content=+1`, and unchanged pages revalidate to `304`, which GitHub
  does not charge against the rate limit — an idle watch can poll for hours for free
- PR activity also includes the special case where `chatgpt-codex-connector[bot]` leaves a `+1` reaction on the PR body instead of posting a comment
- PR activity also includes lifecycle changes on the PR itself, for example draft/ready, closed, reopened, or merged transitions
- self-authored comments are ignored by default when the current authenticated GitHub user can be resolved; pass `--include-self-comments` to keep them
- authentication is preferred; unauthenticated polling is slower and more fragile

New PRs in a repository:

```bash
vibe watch add \
  --name "Watch new PRs" \
  --message "The repository has new pull requests. Review the new PRs and continue as needed." \
  -- \
  uv run --no-project scripts/wait_pr.py \
    --repo avibe-bot/avibe \
    --new-prs \
    --interval 60
```

New issues or issue comments:

```bash
uv run --no-project scripts/wait_issue.py --repo avibe-bot/avibe --new-issues --interval 60
uv run --no-project scripts/wait_issue.py --repo avibe-bot/avibe --issue 157 --interval 60
```

GitHub Actions for a pushed commit:

```bash
vibe watch add \
  --name "Watch CI" \
  --message "GitHub Actions failed. Inspect the result below and fix the failures. Do not describe the fixes in this conversation: end the turn with a silent block unless you are blocked or a decision needs the user." \
  -- \
  uv run --no-project scripts/wait_action.py \
    --repo cyhhao/sub2api \
    --branch main \
    --sha "$HEAD_SHA" \
    --workflow CI \
    --workflow "Security Scan" \
    --only-on-failure \
    --interval 60
```

`--only-on-failure` exits `64` with the no-event marker when every watched workflow succeeded, so a green
build ends the watch silently instead of spending an Agent turn to say so. The
full summary still goes to `stderr`, which the supervisor records in the Avibe log
(`~/.avibe/logs/vibe_remote.log`) beside the watch id; `vibe watch show` reports
that the cycle ran and found nothing, not the summary itself.
Drop the flag when the follow-up should also run on success, for example when a
green build is supposed to trigger a deploy.

## Practical Advice

- Keep messages action-oriented. Tell the next turn what to do with the waiter result.
- In a fix loop, tell the follow-up turn to fix silently. A review-fix loop can run
  many cycles, and a "here is what I changed" message on each one costs tokens and
  interrupts the user with progress they did not ask to see; what they want is the PR
  converging. Avibe sends nothing when a reply contains only a
  `<silent>...</silent>` block, so the turn can resolve the findings, push, re-arm,
  and stay quiet. Keep a user-facing message for the outcomes a human actually needs:
  the review passed, the loop is blocked, or a decision has to be made.
- If this is the first time using `vibe watch add`, read `vibe watch add --help` first; the help text explains both argument syntax and runtime behavior such as how `--message` and waiter stdout become the follow-up Agent Run input.
- Prefer `vibe watch` over ad-hoc detached shells when the wait should survive the current turn cleanly.
- Treat GitHub as just one example waiter, not the main point of the skill.
- If a watch is no longer useful, remove it instead of leaving stale background work behind.
