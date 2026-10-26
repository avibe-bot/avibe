---
name: background-watch-hook
slug: background-watch-hook
description: Run durable `vibe watch` waits for PR reviews, CI, files or external signals, then resume the Agent in the right Session.
version: 0.19.0
---

# Background Watch Hook

Use `vibe watch` for a durable wait that resumes an Agent when an external event
arrives. The waiter detects events; the follow-up Agent verifies live state and
acts. A success candidate is not merge authorization.

## Contract and lifecycle

| Waiter exit | Meaning |
| --- | --- |
| `0` | One **new** reportable event; summary on stdout |
| `75` (or configured retry code) | Still waiting; no Agent turn |
| `64` plus `avibe-watch: no-event` on stderr | Quiet completed cycle: retire once, re-arm forever |
| `124` or other terminal failure | Notify the Agent and stop |

A bare `64` is an error. Keep diagnostics on stderr. A once Watch waiting for its
first event uses a retry code, not `64`. Forever waiters need durable cursors or
edge detection; a persistent true condition is not a new event. Avibe serializes
follow-ups, re-arms after they settle, and pauses rapid repeated events for repair.

- Read `vibe watch add --help` before first use; inspect/update with
  `vibe watch <list|show|update|pause|resume|remove>`.
- `--timeout` bounds each cycle (default six hours); `--lifetime-timeout` bounds
  the whole Watch. Use `0` for an intentionally unbounded PR delivery loop.
- Remove the Watch at close-out. Do not use detached shells for durable waits.

## Session targeting

- Inside an Avibe-injected Agent shell, omitting the target continues this conversation.
- Use `--session-id <id>` only when resuming a different existing Agent Session.
- Use `--create-session --same-scope` for one visible sibling Session; use
  `--create-session-per-run --same-scope` for a separate visible Session per event.
- Use `--scope-id <scopes.id>` instead of `--same-scope` for an explicit scope.
- If `--cwd` is omitted, Avibe uses the command's current working directory.

## Preferred PR + CI watch

Use `--forever` and one owner-specific state file for the whole loop. Seed and
arm before the watched push/review trigger; for a new PR, create it first, then
immediately seed/arm. Keep the same Watch and cursor across all later rounds.

- `wait_pr.py --workflow <actual-name>` combines PR activity with exact-head CI;
  repeat `--workflow` for every required workflow. Omit `--sha` to follow pushes.
- `--coalesce-success`: keep half-success quiet; report one same-head CI/review
  success candidate. Findings, errors and unfamiliar results remain reportable.
- `--actionable-only` drops review triggers and empty envelopes; `--settle 20`
  batches review bursts. New CI failures bypass settle. Final gates belong to
  `pr-delivery-loop`, including the stricter qualification of Codex reactions.
- A Codex review of the current head that stays running for
  `--review-stall-minutes` (default 30, `0` disables) is reported
  as `Codex review stalled`, again each further window it lasts; the follow-up
  re-requests the review as `pr-delivery-loop` says.
- Set `--timeout 0` on both sides of the `--` command separator; six quiet hours
  is not a PR failure. Verify workflow names: missing CI is never green.
- Never reseed or replace its state between rounds. Filters/reporting modes are
  part of cursor ownership; enable them on the initial seed and armed command.
- Cursor corruption, ownership conflicts or write failures require repair with
  evidence preserved, not a fresh baseline. Retryable GitHub failures keep waiting.

Run scripts from the loaded Skill directory, not a guessed installation path:

```bash
BACKGROUND_WATCH_HOOK_DIR="<directory containing the loaded SKILL.md>"
STATE_FILE="$HOME/.avibe/state/watch-cursors/pr-151-review.json"
BRANCH="$(gh pr view 151 --repo avibe-bot/avibe --json headRefName --jq .headRefName)"

uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
  --repo avibe-bot/avibe --pr 151 --branch "$BRANCH" \
  --workflow lint --coalesce-success --actionable-only \
  --state-file "$STATE_FILE" --seed-state

vibe watch add \
  --name "Watch PR 151 review and CI" --forever --timeout 0 \
  --message "PR #151 has an event. Follow pr-delivery-loop; verify live gates, handle findings/errors, and keep half-success silent." \
  -- \
  uv run --no-project "$BACKGROUND_WATCH_HOOK_DIR/scripts/wait_pr.py" \
    --repo avibe-bot/avibe --pr 151 --branch "$BRANCH" \
    --workflow lint --coalesce-success --actionable-only \
    --settle 20 --state-file "$STATE_FILE" --interval 60 --timeout 0
```

After confirming the seeded Watch is live, push or trigger review. Check exactly
one live Watch per owner/PR/concern at each round's end. The state file stages
reports until delivery acknowledgement, so an undelivered report may replay;
never discard it merely to avoid a duplicate.

## Other waits and troubleshooting

Use `wait_pr.py` without CI arguments for PR-only activity; use `wait_action.py`
for standalone Actions and `wait_issue.py` for issues. Do not combine `--new-prs`
with CI options. `--catch-up` and explicit cursors deliberately replay history,
not routine re-arming. Keep Watch messages short and action-oriented; reference
loaded policy rather than restating it on every fire.

[Detailed waiter reference](references/waiters.md) covers alternative commands,
filters, replay, authentication, and cursor/settle internals. Read it only for
those tasks or a concrete failure, not for routine PR monitoring.
