---
name: background-watch-hook
slug: background-watch-hook
description: Use `vibe watch` to run a managed Harness waiter that returns to the same conversation later. Best for reviews, CI, files, logs, and other wait-now-continue-later workflows.
version: 0.8.0
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
- `exit 124`: timeout; still send a timeout follow-up
- other non-zero: failure; no follow-up

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
- Use `--create-session --same-scope` when each follow-up should run in a visible sibling Session under the same Workbench project or IM scope.
- Use `--create-session --scope-id <scopes.id>` when each follow-up should run in a specific existing scope.
- If `--cwd` is omitted while creating a Session, Avibe follows the caller Session cwd.

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
Bundled GitHub waiters use exit code `75` for retryable startup errors such as temporary network failures or GitHub `408/429/5xx` responses.

## GitHub Example Waiter

Use the bundled GitHub waiter only when the watched thing is PR review activity.

One-shot watch:

```bash
vibe watch add \
  --name "Watch PR 151 reviews" \
  --message "PR #151 has new review activity. Fetch the latest review state, summarize actionable items, and continue handling them if needed." \
  -- \
  uv run --no-project scripts/wait_pr.py \
    --repo avibe-bot/avibe \
    --pr 151 \
    --interval 60
```

Catch up on existing activity first:

```bash
vibe watch add \
  --name "Catch up PR 151 reviews" \
  --message "PR #151 already has review activity. Fetch the latest review state and continue handling it if needed." \
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
  --message "PR #151 has new review activity. Fetch the latest review state, summarize actionable items, and continue handling them if needed." \
  -- \
  uv run --no-project scripts/wait_pr.py \
    --repo avibe-bot/avibe \
    --pr 151 \
    --interval 60
```

GitHub-specific notes:

- `--catch-up` reports activity that already exists at startup
- without `--catch-up`, the waiter snapshots current PR activity as the baseline
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
  --message "GitHub Actions finished. Inspect the result below and continue with the deployment or fix failures." \
  -- \
  uv run --no-project scripts/wait_action.py \
    --repo cyhhao/sub2api \
    --branch main \
    --sha "$HEAD_SHA" \
    --workflow CI \
    --workflow "Security Scan" \
    --interval 60
```

## Practical Advice

- Keep messages action-oriented. Tell the next turn what to do with the waiter result.
- If this is the first time using `vibe watch add`, read `vibe watch add --help` first; the help text explains both argument syntax and runtime behavior such as how `--message` and waiter stdout become the follow-up Agent Run input.
- Prefer `vibe watch` over ad-hoc detached shells when the wait should survive the current turn cleanly.
- Treat GitHub as just one example waiter, not the main point of the skill.
- If a watch is no longer useful, remove it instead of leaving stale background work behind.
