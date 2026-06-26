# Status Bubble — Liveness Signals (A + B + C)

Addendum to `agent-progress-status-bubble.md`. Approved by the user via a
phone-width Show Page mockup (2026-06-25).

## Problem

The running footer's `Xs ago` (time since last activity) keeps growing during a
long single operation, so a healthy turn *looks* stuck. We need positive,
monotonically-increasing signals that prove work is still happening.

## Final design

Replace the `working…` word and the standalone `Xs ago` field. The running
footer becomes compact (≈28 chars, one mobile line):

```
start      ⏳ 0s
normal     ⌛ 18s · 6 st · 12.3k tok          body: 🔧 Read: dispatcher.py
long op    ⏳ 2:45 · 6 st · 12.3k tok         body: 🔧 Bash · 2:30   (body ticks)
dead       ⚠️ backend not responding · 2:45
done       ✅ done · 248k tok    (· 1:24 · when show_duration)
```

### A — Current context-window occupancy (footer)
Revised after the user clarified the figure should express **how much of the
current session's context window is in use** (so they can gauge remaining
headroom), NOT cumulative billing. It is a live SNAPSHOT — grows with the
conversation and DROPS after a /compact. Keyed by `session_key`, persists across
turns, kept on the terminal/done footer. Reported via `note_session_tokens(context,
*, total=…)` (SET, not accumulate).

Verified token sources (against installed `claude_agent_sdk 0.2.93` source +
`codex 0.141.0` / openai/codex protocol):
- **Claude** — the **latest** `AssistantMessage.usage` (the raw per-request
  Anthropic usage block) `= input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens + output_tokens` = the full prompt for that request
  (= context) plus the response that joins it. SET on each assistant message →
  tracks live and the last one wins. (`ResultMessage.usage` is the CLI's
  CUMULATIVE turn usage — overstates occupancy — and is NOT used.)
- **Codex** — the `thread/tokenUsage/updated` notification's
  `tokenUsage.last.totalTokens` (the "latest active context size" the Codex CLI's
  own context bar uses); v1 fallback `info.last_token_usage.total_tokens`. NOT
  `tokenUsage.total` (cumulative). The previously-read `turn.usage` does NOT exist
  in the v2 app-server `Turn` object, so that path is removed.
- Display `{n} tok` with `12.3k / 248k / 1.4M` formatting; **omit when 0/unknown**.

### B — Current-action elapsed (body) — the anti-"stuck" signal
- `action_elapsed = now - last_activity_at` (resets on each real emit; heartbeat
  does NOT reset it). Appended to the body action label only when
  `action_elapsed >= 10s` (fixed `_ACTION_TIME_HINT_S`). Re-rendered by the
  heartbeat every ~15s → it keeps climbing during a single long op.
- When `action_elapsed >= no_output_hint_after_s` AND backend not dead, prefix
  the body time with `⚠️ ` ("unusually long"). This **repurposes the existing
  `agent_status_no_output_ms` config** (its old consumer, the footer `ago`
  emphasis, is removed) — no config churn.

### C — Step count (footer)
- `_status_step_count[turn_key]` increments once per real action emit
  (`_render_concise_status`; heartbeat re-renders are excluded). Display `{n} st`,
  omit when 0.

### Footer assembly
- Hourglass cycles `⏳`/`⌛` per render tick (replaces the `working…` dots) — zero
  width, keeps the "alive" motion.
- Running: `{hour} {turn_elapsed}[ · {n} st][ · {tokens}]`.
- Backend dead: unchanged.
- Terminal: `{marker} {reason}[ · {duration} when show_duration][ · {tokens}]`.

## i18n (vibe/i18n)
- Remove `status.working`, `status.ago` (no longer used).
- Add `status.steps` = `{count} st` / `{count} st`, `status.tokens` = `{count} tok` / `{count} tok`.
- Keep `done/stopped/failed/backendUnresponsive`.

## State (dispatcher)
- New: `_status_step_count: dict[str,int]` (turn-key, dropped in `_drop_status_keys`).
- New: `_session_token_total: dict[str,int]` (session-key, persists across turns).

## Out of scope / not done
- No new Web UI setting (no new knob).
- Codex live within-turn token growth (only per-turn at `turn/completed`); the
  body action-time covers liveness during a long Codex op.

## Slices
1. Dispatcher render + state + i18n + `note_session_tokens` + step counting + tests.
2. Claude token plumbing + test.
3. Codex token plumbing + test.
4. opus + codex acceptance; ruff; amend into the single commit.

## Result delivery: delete bubble + fresh result (notifications)

Editing the bubble into the final result means IM platforms fire NO push
notification for the result (edits don't notify). So the result is now ALWAYS
delivered as a NEW message and the transient bubble is retired:

- In-progress: post the bubble, edit it in place during the turn (unchanged).
- Result: send the answer as a fresh message (inline / split / summary) so the IM
  notifies. The fresh result carries the `✅ done · {tokens}` footer as platform
  subtext (Slack context block, Discord `-#`), so the outcome + context occupancy
  survive the bubble's deletion. `subtext` is now a first-class optional on
  `send_message_with_buttons` (Slack/Discord) and Slack `send_markdown_message`,
  passed only when set so non-bubble adapters are unaffected.
- Retire the bubble via `_retire_status_bubble`: DELETE it when the platform
  supports deletion (`PlatformCapabilities.supports_message_deletion`, new) and
  the result was delivered; otherwise fall back to collapsing it to a terminal
  marker (the prior behavior) so it never lingers as "running". Send-then-delete
  ordering: a failed send keeps the bubble (collapsed), never an empty void.
- New `delete_message(context, message_id) -> bool`: base default no-op (False),
  Slack `chat.delete`, Discord fetch+delete; Telegram already had it.
- Removed: `_edit_bubble_into_result` and the split-path bubble reuse
  (`edit_first_message_id`) — both obsolete under delete+resend. Also fixed a
  latent drift where the summary-path `.md` attachment ran for all result paths;
  it is back inside the summary `else` branch only.
